#!/bin/bash
# build-nut-master.sh -- build NUT master's usbhid-ups driver (+ upsdrvctl)
# ON THE JETSON, as the normal user, for src/jetson/nut-driver-upgrade to
# install. Only the driver is swapped: the distro 2.7.4 upsd/upsmon stay, and
# the driver<->upsd socket protocol is backward compatible
# (docs/UPS-TOOLING.md s5). Takes ~10 min on a Nano in 5 W mode.
#
#   mkdir -p ~/build && cd ~/build
#   git clone --depth 50 https://github.com/networkupstools/nut.git
#   ./build-nut-master.sh        # then: sudo nut-driver-upgrade install
#
# Tested at master a66c009 (2.8.5.1-dev): it includes PR #3566 (shutdown.return
# -> APCDelayBeforeReboot = 1) and #3550 (the reconnect segfault fix that
# stock 2.8.5 lacks). Needs: build-essential autoconf automake libtool
# pkg-config libusb-1.0-0-dev.
set -e
cd ~/build/nut
./autogen.sh
./configure --prefix=/opt/nut-master --sysconfdir=/etc/nut \
  --with-statepath=/run/nut --with-altpidpath=/run/nut --with-pidpath=/run/nut \
  --with-drvpath=/opt/nut-master/driver --with-user=nut --with-group=nut \
  --with-usb=libusb-1.0 --with-drivers=usbhid-ups \
  --with-doc=no --with-dev=no --with-cgi=no --with-snmp=no --with-neon=no \
  --with-nss=no --with-openssl=no --with-wrap=no --with-avahi=no \
  --with-ipmi=no --with-powerman=no --with-modbus=no --with-linux_i2c=no \
  --with-nut-scanner=no --with-systemdsystemunitdir=no --with-python=no --with-python2=no --with-python3=no
make -j3
echo BUILD-OK
