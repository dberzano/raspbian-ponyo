much fun

Configure Raspbian with Ansible
===============================

Ansible configuration for my Raspberry Pi Zero W.


Install the OS
--------------
Use the [Raspberry Pi Imager](https://www.raspberrypi.org/downloads/) that does the job for you.
Select the "Lite" version of the operating system.


Headless setup
--------------
The setup is completely headless. This means we need to enable SSH and the Wi-Fi configuration
somehow. [Here](https://desertbot.io/blog/headless-raspberry-pi-3-bplus-ssh-wifi-setup) you can find
some helpful resources.

To enable SSH, create an empty `/ssh` in the SD card. To enable Wi-Fi, create a file named
`/wpa_supplicant.conf` with the following configuration:

```
country=FR
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1

network={
    scan_ssid=1
    ssid="Name Of Your Wi-Fi Network"
    psk="Your Wi-Fi Password"
}
```

Note that the value `scan_ssid=1` is essential if your Wi-Fi network does not broadcast its SSID
(_i.e._ it's a "hidden" network). This is normally the case in a dual 5 GHz/2.4 GHz setup to prevent
high-speed devices to connect to the 2.4 GHz network. The Raspberry Pi Zero W only has a 2.4 GHz
capable Wi-Fi chipset.

It is important to take note of the MAC address of your board. If your router allows it, pin it to
a certain IP address so that it's easier to access.

Test the setup with:

```
ssh pi@192.168.0.50
```

The default password is `raspberry` and the `pi` account is sudo-enabled. A prompt will greet you
advising to change your default password.

From now on, the setup is completely done through Ansible. This also means that the setup is fully
reproducible.


First run
---------
The first Ansible run is for setting up the SSH key. We need to use the password for that. Ansible
requires `sshpass` to be installed, and Homebrew if running on macOS does not make it easy to
install that package.

Oh well, you can do the following on your macOS provisioner:

```
brew install https://raw.githubusercontent.com/kadwanev/bigboybrew/master/Library/Formula/sshpass.rb
```

Now run the first-time Ansible setup:

```
ansible-playbook provision.yml -i 192.168.0.50, -k
```

It will ask you for the default `pi` password, which is `raspberry` as said.

After fully running, it will be **impossible** to log back into the Raspberry Pi using the password,
as it has been disabled. From now on, only SSH access through private key is allowed. `sudo` will
require no password. The private/public key pair was generated in
[Ed25519](https://medium.com/risan/upgrade-your-ssh-key-to-ed25519-c6e8d60d3c54) using the following
command:

```
ssh-keygen -t ed25519 -a 100 -o -f mykey
```

If by chance re-running `provision.yml` finishes successfully, it just means that it opened a
persistent SSH connection in the past which is still active. Remove `~/.ansible` on your provisioner
to force Ansible to connect anew.

The file `group_vars/all` contains the private counterpart of the installed public key. Configure it
on your provisioner's `~/.ssh/config` appropriately.


Ordinary runs
-------------

Note that this assumes your provisioner's SSH client has been configured properly.

Just run (given you have assigned the SSH nickname `ponyo` to your Raspberry Pi):

```
ansible-playbook site.yml -i ponyo,
```