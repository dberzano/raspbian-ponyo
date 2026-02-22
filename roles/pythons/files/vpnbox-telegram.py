#!/opt/python/globalvenv/bin/pythonglobalvenv.sh

import asyncio
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml
from flag import FlagError, flag_safe
from pydantic import BaseModel, ConfigDict, Secret, model_validator
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyParameters,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from typing_extensions import Self


class BotConfig(BaseModel):
    """Configuration schema."""

    telegram_api_key: Secret[str]
    authorized_chat_ids: list[int]
    vpn_flavours: list[str]
    netplan_dir: Path = Path("/etc/netplan")
    netplan_writable_dir: Path = Path("/var/tmp/dynamic_netplan_config")
    netplan_config_dest: str = "routerino.yaml"
    netplan_config_src: str = "routerino.yaml_all_networks"

    wifi_iface: str = "wlan0"
    bssid_nicknames: dict[str, str] = {}

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def autofill_vpn_flavours(cls, data: Any) -> Any:
        """Autofill VPN flavours from installed binaries."""
        vpn_flavours = data.get("vpn_flavours", [])
        if not vpn_flavours:
            vpn_flavours = sorted(
                x.name.removeprefix("vpnbox-")
                for x in Path("/usr/local/sbin").glob("vpnbox-*")
            )
            data["vpn_flavours"] = vpn_flavours
        return data

    @model_validator(mode="after")
    def check_vpn_flavours(self) -> Self:
        """Check if we have at least one VPN flavour."""
        if not self.vpn_flavours:
            raise ValueError("vpn_flavours must have at least one entry")
        return self


# Use this logger for all message printouts
LOGGER: logging.Logger = None

# Configuration
CONF: Optional[BotConfig] = None


class FilterAuthorizedChatId(filters.BaseFilter):
    def __init__(self, authorized_ids: Iterable[int]) -> None:
        """Construct the custom filter."""
        super().__init__("filter_authorized_chat_id", data_filter=False)
        self._authorized_ids = authorized_ids

    def check_update(self, update: Update) -> bool:
        return update.effective_chat.id in self._authorized_ids


def _init_logger() -> None:
    """Initialize logger for all."""
    global LOGGER

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    LOGGER = logging.getLogger("svizzerino")
    LOGGER.setLevel(logging.DEBUG)
    svizzerino_handler = logging.StreamHandler()
    svizzerino_handler.setLevel(logging.DEBUG)
    svizzerino_handler.setFormatter(formatter)
    LOGGER.addHandler(svizzerino_handler)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.WARNING)
    root_handler = logging.StreamHandler()
    root_handler.setLevel(logging.WARNING)
    root_handler.setFormatter(formatter)
    root_logger.addHandler(root_handler)


def _init_config() -> None:
    """Load configuration or fail."""
    global CONF
    conf_files = [
        Path.cwd() / ".vpnbox-telegram-config.json",
        Path.home() / ".vpnbox-telegram-config.json",
        Path("/opt/telegramsecrets/vpnbox-telegram"),
    ]
    exc = None
    for c in conf_files:
        LOGGER.debug(f"trying to load configuration {c}")
        try:
            CONF = BotConfig.model_validate_json(c.read_text())
            exc = None
            break
        except Exception as e:
            LOGGER.error(f"failed loading {c}: {type(e).__name__} - {str(e)}")
            exc = e
    if exc is not None:
        raise exc
    LOGGER.info(f"configuration loaded from {c}")
    LOGGER.info(f"VPN flavours: {', '.join(CONF.vpn_flavours)}")


def _init_netplan() -> None:
    """Make it possible to write under the netplan configuration dir.

    We do the following operations but mostly in Python:

    ```sh
    rm -rf /var/tmp/dynamic_netplan_config
    umount /etc/netplan || true
    mkdir /var/tmp/dynamic_netplan_config
    rsync -a --delete /etc/netplan/ /var/tmp/dynamic_netplan_config/
    mount -o bind /var/tmp/dynamic_netplan_config /etc/netplan
    ```
    """
    # Remove original directory
    LOGGER.debug(f"removing {CONF.netplan_writable_dir}")
    shutil.rmtree(CONF.netplan_writable_dir, ignore_errors=True)

    # Umount the netplan directory's bind mount, ignoring errors
    LOGGER.debug(f"umounting bind-mounted {CONF.netplan_dir}")
    subprocess.run(
        ["umount", CONF.netplan_dir.as_posix()],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Create the writable directory (not ok if exists or parents do not exist)
    LOGGER.debug(f"creating writable netplan config dir: {CONF.netplan_writable_dir}")
    CONF.netplan_writable_dir.mkdir()

    # Copy the contents of the original directory to the writable one using glob
    for f in CONF.netplan_dir.glob("*"):
        LOGGER.debug(f"copying {f} -> {CONF.netplan_writable_dir}")
        shutil.copy(f, CONF.netplan_writable_dir)

    # Bind mount the writable directory to the original one
    LOGGER.debug(f"bind-mounting {CONF.netplan_writable_dir} to {CONF.netplan_dir}")
    subprocess.run(
        [
            "mount",
            "-o",
            "bind",
            CONF.netplan_writable_dir.as_posix(),
            CONF.netplan_dir.as_posix(),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    LOGGER.info("successfully initialized netplan configuration")


def _print_initial_message(application: Application, when: int = 0):
    """Print message at startup using the asyncio paradigm."""

    async def _handle_print_event(context: ContextTypes.DEFAULT_TYPE) -> None:
        """Actually prints the message."""
        job = context.job
        text, markup = await _prepare_vpn_menu()
        await context.bot.send_message(
            job.chat_id,
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    for cid in CONF.authorized_chat_ids:
        LOGGER.info(f"printing welcome message for chat ID {cid}")
        application.job_queue.run_once(
            _handle_print_event,
            when=when,
            chat_id=cid,
        )


def _flavour_to_flag(flavour: str) -> str:
    """Convert flavour to the corresponding Unicode flag if possible.

    For WireGuard flavours (wg-XX), the country code is extracted from after
    the ``wg-`` prefix. Since the protocol is already clear from the menu
    context, only the flag is returned (no ``(wg)`` suffix).
    """
    flavour = flavour.lower()
    if flavour in ("disconnect", "ssh_from_anywhere"):
        return flavour

    # WireGuard flavours are prefixed with "wg-": extract the country code
    country_code = flavour[3:] if flavour.startswith("wg-") else flavour

    try:
        country_flag = flag_safe(country_code[0:2])
    except FlagError:
        return f"{flavour} (cannot find flag)"
    if len(country_code) == 2:
        return country_flag
    else:
        return f"{country_flag} ({flavour})"


@dataclass
class WifiInfo:
    """WiFi connection information."""

    essid: str
    bssid: str  # lowercase with colons
    signal_dbm: int
    link_quality_current: int
    link_quality_max: int
    frequency_ghz: float
    bit_rate: float
    bit_rate_unit: str  # Kb/s, Mb/s, or Gb/s

    @property
    def link_quality_percent(self) -> int:
        """Calculate link quality as a percentage."""
        if self.link_quality_max == 0:
            return 0
        return int((self.link_quality_current / self.link_quality_max) * 100)


async def _get_wifi_info() -> Optional[WifiInfo]:
    """Get detailed WiFi connection information using iwconfig."""
    try:
        aprocess = await asyncio.create_subprocess_exec(
            "iwconfig",
            CONF.wifi_iface,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return None

    stdout, _ = await aprocess.communicate()
    output = stdout.decode("utf-8")

    # Check if not connected
    if "ESSID:off/any" in output or 'ESSID:""' in output:
        return None

    # Parse ESSID (handles escaped quotes in network name)
    essid_match = re.search(r'ESSID:"((?:[^"\\]|\\.)+)"', output)
    if not essid_match:
        return None
    essid = essid_match.group(1)

    # Parse BSSID (Access Point)
    bssid_match = re.search(r"Access Point: ([0-9A-Fa-f:]+)", output)
    if not bssid_match:
        return None
    bssid = bssid_match.group(1).lower()

    # Parse Signal level
    signal_match = re.search(r"Signal level=(-?\d+) dBm", output)
    if not signal_match:
        return None
    signal_dbm = int(signal_match.group(1))

    # Parse Link Quality
    quality_match = re.search(r"Link Quality=(\d+)/(\d+)", output)
    if not quality_match:
        return None
    link_quality_current = int(quality_match.group(1))
    link_quality_max = int(quality_match.group(2))

    # Parse Frequency
    freq_match = re.search(r"Frequency:(\d+\.\d+) GHz", output)
    if not freq_match:
        return None
    frequency_ghz = float(freq_match.group(1))

    # Parse Bit Rate (flexible: accepts any unit ending with /s)
    bitrate_match = re.search(r"Bit Rate[=:]([\d.]+) (\S+/s)", output)
    if not bitrate_match:
        return None
    bit_rate = float(bitrate_match.group(1))
    bit_rate_unit = bitrate_match.group(2)

    return WifiInfo(
        essid=essid,
        bssid=bssid,
        signal_dbm=signal_dbm,
        link_quality_current=link_quality_current,
        link_quality_max=link_quality_max,
        frequency_ghz=frequency_ghz,
        bit_rate=bit_rate,
        bit_rate_unit=bit_rate_unit,
    )


async def _prepare_vpn_menu() -> tuple[str, InlineKeyboardMarkup]:
    """Prepare the top-level VPN menu (level 1): pick a protocol."""
    keyboard = []

    # Protocol picker row: always show both, even if one has 0 countries
    keyboard.append(
        [
            InlineKeyboardButton("OpenVPN", callback_data="protocol:openvpn"),
            InlineKeyboardButton("WireGuard", callback_data="protocol:wireguard"),
        ]
    )

    # Disconnect / Cancel row
    keyboard.append(
        [
            InlineKeyboardButton("❌ Disconnect", callback_data="disconnect"),
            InlineKeyboardButton("🚫 Cancel", callback_data="cancel"),
        ]
    )

    # SSH row
    keyboard.append(
        [
            InlineKeyboardButton(
                "🔒 SSH from anywhere", callback_data="ssh_from_anywhere"
            ),
        ]
    )

    # Present a list of known WiFi networks to connect to as buttons
    for net in await get_list_of_known_wifi_networks():
        keyboard.append(
            [InlineKeyboardButton(f"🛜 {net}", callback_data=f"wifi:{net}")]
        )

    reply_markup = InlineKeyboardMarkup(keyboard)

    # Get WiFi connection information
    wifi_info = await _get_wifi_info()
    if wifi_info is not None:
        # Format the WiFi info message
        signal_bar = _format_signal_bar(wifi_info.link_quality_percent)
        # Escape bit rate (decimal point and unit need escaping)
        bit_rate_str = telegram_escape(
            f"{wifi_info.bit_rate} {wifi_info.bit_rate_unit}"
        )
        # Escape frequency (decimal point needs escaping)
        freq_str = telegram_escape(f"{wifi_info.frequency_ghz} GHz")

        # Build nickname line if BSSID is mapped
        nickname = CONF.bssid_nicknames.get(wifi_info.bssid.lower())
        nickname_line = f"✨ {telegram_escape(nickname)}\n" if nickname else ""

        # Build complete message
        msg = (
            f"Connected to WiFi network:\n\n"
            f"🛜 {telegram_escape(wifi_info.essid)}\n"
            f"{nickname_line}"
            f"⚙️ `{telegram_escape(wifi_info.bssid)}`\n"
            f"🌊 {freq_str}\n"
            f"💪 {signal_bar} {wifi_info.link_quality_percent}\%\n"
            f"🐌 {bit_rate_str}\n\n"
            f"Pick a VPN protocol:"
        )
    else:
        msg = "Not connected to a wifi network\\. Pick a VPN protocol:"
    return msg, reply_markup


def _prepare_country_menu(protocol: str) -> tuple[str, InlineKeyboardMarkup]:
    """Prepare the country picker menu (level 2) for a given protocol.

    Args:
        protocol: Either ``"openvpn"`` or ``"wireguard"``.
    """
    if protocol == "wireguard":
        flavours = [f for f in CONF.vpn_flavours if f.startswith("wg-")]
        label = "WireGuard"
    else:
        flavours = [f for f in CONF.vpn_flavours if not f.startswith("wg-")]
        label = "OpenVPN"

    keyboard = []

    if flavours:
        keyboard.append(
            [
                InlineKeyboardButton(
                    _flavour_to_flag(flavour), callback_data=f"connect:{flavour}"
                )
                for flavour in flavours
            ]
        )

    # Back button
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="back")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    if flavours:
        msg = f"Pick a country \\({telegram_escape(label)}\\):"
    else:
        msg = f"No countries configured for {telegram_escape(label)}\\."

    return msg, reply_markup


async def handle_cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    LOGGER.info(f"user {update.effective_user} has issued command /start")
    text, markup = await _prepare_vpn_menu()
    await update.message.reply_text(
        text=text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN_V2
    )


def telegram_escape(s: str) -> str:
    """Escape some Telegram text."""
    out = s
    for c in "\\*_[]()~>#+-=|{}.!`":  # note that the backslash must be the first one
        out = out.replace(c, f"\\{c}")
    return out


def _format_signal_bar(percent: int) -> str:
    """Format signal strength as a bar (0-100% -> ▱▱▱▱▱ to ▰▰▰▰▰)."""
    # Clamp to 0-100
    percent = max(0, min(100, percent))

    # Calculate filled bars (5 total bars)
    filled = int(percent / 20)
    empty = 5 - filled

    return "▰" * filled + "▱" * empty


async def _reconnect_telegram(
    context: ContextTypes.DEFAULT_TYPE, redisplay_start: bool
) -> None:
    """Reconnect to Telegram after network change.

    Args:
        context: Telegram context
        redisplay_start: If True, display the start menu after reconnection
    """
    LOGGER.info("reconnecting to Telegram after network change")
    try:
        await context.application.updater.stop()
        await context.application.updater.start_polling(
            allowed_updates=Update.ALL_TYPES
        )
        LOGGER.info("successfully reconnected to Telegram")

        if redisplay_start:
            job = context.job
            text, markup = await _prepare_vpn_menu()
            await context.bot.send_message(
                job.chat_id,
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.MARKDOWN_V2,
            )
    except Exception as e:
        LOGGER.error(
            f"failed to reconnect to Telegram: {e.__class__.__name__}: {str(e)}"
        )


async def handle_reply_to_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Parses the CallbackQuery and updates the message text - this connects to the VPNs."""
    query = update.callback_query
    LOGGER.info(
        f"user {update.callback_query.from_user} replied to /start - query data is {query.data}"
    )

    # CallbackQueries need to be answered, even if no notification to the user is needed
    # Some clients may have trouble otherwise. See https://core.telegram.org/bots/api#callbackquery
    await query.answer()

    if query.data == "cancel":
        await query.delete_message()
        return

    if query.data.startswith("wifi:"):
        connect_to = query.data.split(":", 1)[1]
        await query.edit_message_text(
            text=f"🛜 Requested connection to WiFi: *{telegram_escape(connect_to)}*, you will be notified if successful",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await connect_to_wifi_network(connect_to, context, query.message.chat_id)
        return

    # Level 1 → Level 2: user picked a protocol, show country picker
    if query.data.startswith("protocol:"):
        protocol = query.data.split(":", 1)[1]
        LOGGER.debug(f"user {query.from_user} picked protocol {protocol}")
        text, markup = _prepare_country_menu(protocol)
        await query.edit_message_text(
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # Level 2 → Level 1: user pressed Back
    if query.data == "back":
        LOGGER.debug(f"user {query.from_user} pressed Back, showing top-level menu")
        text, markup = await _prepare_vpn_menu()
        await query.edit_message_text(
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # From here on, all actions run an external command — show a "please wait" message
    await query.edit_message_text(
        text=f"⏳ You have selected *{telegram_escape(_flavour_to_flag(query.data.removeprefix('connect:')))}* \\- please wait",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    is_vpn_operation = False
    if query.data == "disconnect":
        LOGGER.debug(
            f"user {query.from_user} has requested disconnection from all VPNs"
        )

        # Build disconnect commands: one for OpenVPN and one for WireGuard (if available)
        ovpn_flavours = [f for f in CONF.vpn_flavours if not f.startswith("wg-")]
        wg_flavours = [f for f in CONF.vpn_flavours if f.startswith("wg-")]
        disconnect_cmds = []
        if ovpn_flavours:
            disconnect_cmds.append([f"vpnbox-{ovpn_flavours[0]}", "--disconnect"])
        if wg_flavours:
            disconnect_cmds.append([f"vpnbox-{wg_flavours[0]}", "--disconnect"])

        all_output = []
        overall_ok = True
        for cmd in disconnect_cmds:
            LOGGER.debug(f"running disconnect command: {' '.join(cmd)}")
            try:
                aprocess = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except FileNotFoundError:
                all_output.append(f"[{cmd[0]}] executable not found")
                overall_ok = False
                continue
            stdout, _ = await aprocess.communicate()
            all_output.append(stdout.decode("utf-8"))
            if aprocess.returncode != 0:
                overall_ok = False

        escaped_out = telegram_escape("\n".join(all_output))
        emoji = "✅" if overall_ok else "❌"

        if overall_ok:
            await _reconnect_telegram(context, redisplay_start=False)

        await query.message.reply_text(
            text=f"{emoji} Disconnect \\- output:\n\n```\n{escaped_out}```",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_parameters=ReplyParameters(message_id=query.message.message_id),
        )
        return

    if query.data == "ssh_from_anywhere":
        LOGGER.debug(f"user {query.from_user} has requested remote SSH access")
        cmd = [
            "bash",
            "-c",
            (
                "tmate -S /tmp/tmate.sock kill-server &> /dev/null ; "
                "tmate -S /tmp/tmate.sock new-session -d && "
                "tmate -S /tmp/tmate.sock wait tmate-ready && "
                "tmate -S /tmp/tmate.sock display -p '#{tmate_ssh}'"
            ),
        ]
    elif query.data.startswith("connect:"):
        flavour = query.data.split(":", 1)[1]
        LOGGER.debug(
            f"user {query.from_user} has requested connection to VPN flavor {flavour}"
        )
        cmd = [f"vpnbox-{flavour}", "--connect"]
        is_vpn_operation = True
    else:
        LOGGER.warning(f"unhandled callback data: {query.data}")
        await query.edit_message_text(
            text="❌ Unknown action",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    try:
        aprocess = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        await query.edit_message_text(
            text=f"❌ Cannot find executable to run",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    stdout, _ = await aprocess.communicate()

    escaped_out = telegram_escape(stdout.decode("utf-8"))
    emoji = "✅" if aprocess.returncode == 0 else "❌"

    # Reconnect immediately if VPN operation succeeded
    if is_vpn_operation and aprocess.returncode == 0:
        await _reconnect_telegram(context, redisplay_start=False)

    LOGGER.debug(
        f"cmd {' '.join(cmd)} finished with exitcode {aprocess.returncode} - sending reply"
    )
    await query.message.reply_text(
        text=f"{emoji} Returned `{aprocess.returncode}` \\- output:\n\n```\n{escaped_out}```",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_parameters=ReplyParameters(message_id=query.message.message_id),
    )


async def handle_unauthorized(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle all unauthorized users."""
    LOGGER.warning(f"a message came from an unauthorized party - ignoring: {update}")
    await update.message.reply_text("you are not authorized to interact with this bot")


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all unauthorized users."""
    LOGGER.warning(
        f"user {update.effective_chat.id} is authorized but wrote something unhandled"
    )
    await update.message.reply_text("I do not understand")


async def connect_to_wifi_network(
    wifi_network: str, context: ContextTypes.DEFAULT_TYPE, chat_id: int
) -> None:
    """Change the netplan configuration so that it contains a single wifi network."""
    # Open netplan configuration
    netplan_config_read = CONF.netplan_writable_dir / CONF.netplan_config_src
    netplan_config_write = CONF.netplan_writable_dir / CONF.netplan_config_dest
    try:
        # Read configuration first
        LOGGER.debug(f"reading netplan configuration from {netplan_config_read}")
        with open(netplan_config_read) as f:
            data = yaml.safe_load(f)
        data["network"]["wifis"][CONF.wifi_iface]["access-points"] = {
            name: content
            for name, content in data["network"]["wifis"][CONF.wifi_iface][
                "access-points"
            ].items()
            if name == wifi_network
        }

        # Write configuration
        LOGGER.debug(f"writing netplan configuration to {netplan_config_write}")
        with open(netplan_config_write, "w") as f:
            yaml.safe_dump(data, f)

        # Apply configuration
        LOGGER.debug(f"applying netplan configuration")
        aprocess = await asyncio.create_subprocess_exec(
            "netplan",
            "apply",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await aprocess.communicate()

        LOGGER.info(f"applied netplan configuration for WiFi network {wifi_network}")

        # Schedule reconnection with menu display after 10 seconds
        LOGGER.info("scheduling Telegram reconnection and menu display in 10 seconds")
        context.job_queue.run_once(
            lambda ctx: _reconnect_telegram(ctx, redisplay_start=True),
            when=10,
            chat_id=chat_id,
        )

    except Exception as e:
        LOGGER.error(
            f"unable to connect to WiFi network - {e.__class__.__name__}: {str(e)}"
        )
        return


async def get_list_of_known_wifi_networks() -> set[str]:
    """Run `iwlist wlan0 scan` and get the list of ESSIDs only."""
    try:
        aprocess = await asyncio.create_subprocess_exec(
            "iwlist",
            CONF.wifi_iface,
            "scan",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await aprocess.communicate()

        output = stdout.decode("utf-8")
        essids = set(re.findall(r'ESSID:"(.*?)"', output))
        # return essids
    except Exception as e:
        LOGGER.error(
            f"unable to get the list of WiFi networks - {e.__class__.__name__}: {str(e)}"
        )
        return set()

    # Open the netplan file and return a list of known ESSIDs
    try:
        with open(CONF.netplan_writable_dir / CONF.netplan_config_src) as f:
            data = yaml.safe_load(f)
        known_essids = data["network"]["wifis"][CONF.wifi_iface]["access-points"].keys()
        del data
    except Exception as e:
        LOGGER.error(f"unable to read netplan file - {e.__class__.__name__}: {str(e)}")
        return set()

    # We need to return only the essids present in known_essids by using set() operations
    return essids & known_essids


def main():
    """Entry point."""
    _init_logger()
    _init_config()
    _init_netplan()

    LOGGER.info("initializing Telegram bot")
    application_builder = Application.builder()
    application = application_builder.token(
        CONF.telegram_api_key.get_secret_value()
    ).build()
    authorized = FilterAuthorizedChatId(
        CONF.authorized_chat_ids
    )  # only authorized IDs can chat

    # Add a handler for the /start command
    application.add_handler(
        CommandHandler(command="start", callback=handle_cmd_start, filters=authorized)
    )

    # Add a handler for the replies to the start command
    application.add_handler(CallbackQueryHandler(handle_reply_to_start))

    # Handle everything else (unknown)
    application.add_handler(
        MessageHandler(
            filters=(filters.ALL & authorized),
            callback=handle_unknown,
        )
    )

    # Handle everything else (not authorized) - note: only one handler will trigger (fallback)
    application.add_handler(
        MessageHandler(
            filters=(filters.TEXT & ~filters.COMMAND),
            callback=handle_unauthorized,
        )
    )

    _print_initial_message(application)

    # Blocking call - run all the events and start listening for commands
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    # Application's entry point
    main()
