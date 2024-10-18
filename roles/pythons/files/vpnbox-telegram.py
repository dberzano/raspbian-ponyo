#!/opt/python/globalvenv/bin/pythonglobalvenv.sh

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml
from flag import FlagError, flag_safe
from pydantic import BaseModel, ConfigDict, Secret, model_validator
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
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
    netplan_config: Path = Path("/etc/netplan/routerino.yaml")
    wifi_iface: str = "wlan0"

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def autofill_vpn_flavours(cls, data: Any) -> Any:
        """Autofill VPN flavours from installed binaries."""
        vpn_flavours = data.get("vpn_flavours", [])
        if not vpn_flavours:
            vpn_flavours = sorted(
                x.name.rsplit("-", 1)[1] for x in Path("/usr/local/sbin").glob("vpnbox-*")
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

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

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


def _print_initial_message(application: Application):
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
            when=0,
            chat_id=cid,
        )


def _flavour_to_flag(flavour: str) -> str:
    """Convert flavour to the corresponding Unicode flag if possible."""
    try:
        flavour_flag = flag_safe(flavour[0:2])
    except FlagError:
        # Cannot convert to flag: return it as is, but uppercased
        return flavour.upper()
    if len(flavour) == 2:
        # Name is fully constituted by the country code: return flag
        return flavour_flag
    else:
        # Name contains country code and something else: return flag and description
        return f"{flavour_flag} ({flavour.upper()})"


async def _get_wifi_name() -> Optional[str]:
    """Get the name of the connected wifi, or None if not connected."""
    try:
        aprocess = await asyncio.create_subprocess_exec(
            "iwgetid",
            "--raw",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return None

    stdout, _ = await aprocess.communicate()
    return stdout.decode("utf-8").strip()


async def _prepare_vpn_menu() -> tuple[str, ReplyKeyboardMarkup]:
    """Prepare the VPN menu with the buttons."""
    # Standard buttons for picking a VPN flavour and disconnecting
    row1 = [
        InlineKeyboardButton(_flavour_to_flag(flavour), callback_data=flavour)
        for flavour in CONF.vpn_flavours
    ]
    row2 = [
        InlineKeyboardButton("❌ Disconnect", callback_data="disconnect"),
        InlineKeyboardButton("🚫 Cancel", callback_data="cancel"),
    ]
    keyboard = [row1, row2]

    # Present a list of known WiFi networks to connect to as buttons
    for net in await get_list_of_known_wifi_networks():
        keyboard.append([InlineKeyboardButton(f"📡 {net}", callback_data=f"cancel")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    # Run iwgetid to get the SSID of the connected wifi
    wifi_ssid = await _get_wifi_name()
    if wifi_ssid is not None:
        wifi_ssid.replace(".", "\\.")
        msg = f"Connected to wifi network *{wifi_ssid}*\\. Pick a VPN variant:"
    else:
        msg = "Not connected to a wifi network\\. Pick a VPN variant:"
    return msg, reply_markup


async def handle_cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /start is issued."""
    LOGGER.info(f"user {update.effective_user} has issued command /start")
    text, markup = await _prepare_vpn_menu()
    await update.message.reply_text(
        text=text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN_V2
    )


async def handle_reply_to_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    await query.edit_message_text(
        text=f"⏳ You have selected: {_flavour_to_flag(query.data)}, please wait"
    )

    if query.data == "disconnect":
        LOGGER.debug(f"user {query.from_user} has requested disconnection from all VPNs")
        cmd = [f"vpnbox-{CONF.vpn_flavours[0]}", "--disconnect"]
    else:
        LOGGER.debug(f"user {query.from_user} has requested connection to VPN flavor {query.data}")
        cmd = [f"vpnbox-{query.data}", "--connect"]

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

    escaped_out = stdout.decode("utf-8").replace("`", "\\`")
    emoji = "✅" if aprocess.returncode == 0 else "❌"

    LOGGER.debug(f"cmd {' '.join(cmd)} finished with exitcode {aprocess.returncode} - updating msg")
    await query.edit_message_text(
        text=f"{emoji} Exitcode `{aprocess.returncode}` \\- output:\n\n```\n{escaped_out}```",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def handle_unauthorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all unauthorized users."""
    LOGGER.warning(f"a message came from an unauthorized party - ignoring: {update}")
    await update.message.reply_text("you are not authorized to interact with this bot")


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all unauthorized users."""
    LOGGER.warning(f"user {update.effective_chat.id} is authorized but wrote something unhandled")
    await update.message.reply_text("I do not understand")


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
        LOGGER.error(f"unable to get the list of WiFi networks - {e.__class__.__name__}: {str(e)}")
        return []

    # Open the netplan file and return a list of known ESSIDs
    netplan_config_all = CONF.netplan_config.with_suffix(".yaml_all_networks")
    try:
        with open(netplan_config_all) as f:
            data = yaml.safe_load(f)
        known_essids = data["network"]["wifis"][CONF.wifi_iface]["access-points"].keys()
        del data
    except Exception as e:
        LOGGER.error(f"unable to read netplan file - {e.__class__.__name__}: {str(e)}")
        return []

    # We need to return only the essids present in known_essids by using set() operations
    return essids & known_essids


def main():
    """Entry point."""
    _init_logger()
    _init_config()

    LOGGER.info("initializing Telegram bot")
    application_builder = Application.builder()
    application = application_builder.token(CONF.telegram_api_key.get_secret_value()).build()
    authorized = FilterAuthorizedChatId(CONF.authorized_chat_ids)  # only authorized IDs can chat

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
