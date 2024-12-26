Configure Orange Pi with Ansible
================================

We are using [this](https://github.com/Joshua-Riek/ubuntu-rockchip?tab=readme-ov-file) as a
reference, with the Ubuntu Server image from
[this page](https://joshua-riek.github.io/ubuntu-rockchip-download/boards/orangepi-3b.html). Image
is downloaded, verified, uncompressed and dumped on the SD card with a simple `dd` command.


Manual login
------------
You can connect your Orange Pi with your Mac using Connection Sharing, and then boot it. It will get
an IP address from the Ethernet card in the range 192.168.2.0/24 in most cases (find it with the aid
of the Console).

```sh
ssh ubuntu@192.168.2.2
```

The default password is `ubuntu` and you are prompted to change it at the first login. Root user is
disabled for logins, but you can use `sudo`.


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

```sh
ansible-playbook provision.yml -i 192.168.2.2, -k
```

It will ask you for the default `ubuntu` password, which is `ubuntu` as said.

After fully running, it will be **impossible** to log back into the Orange Pi 3B using the password,
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

Just run (given you have assigned the SSH nickname `svizzerino` to your Orange Pi 3B):

```sh
ansible-playbook site.yml -i svizzerino,
```
