#!/bin/bash

set -e

# execute as root
if [ "$LOGNAME" != "root" ]; then
    echo Please execute as root or by using sudo
    echo ':('
    exit 1
fi


# questions
echo -n "Hostname of the cluster node : "
read hostname
echo -n "public ip : "
read public_ip
echo -n "private ip : "
read private_ip
echo -n "address of the SAN (such as san-hdd-14.rpn.online.net): "
read san_address


# backup original cloud-config file
CLOUD_CONFIG=/var/lib/coreos-install/user_data
if [ ! -e $CLOUD_CONFIG.bkp ]; then
    echo Creating backup of original config in $CLOUD_CONFIG.bkp...
    cp -a $CLOUD_CONFIG $CLOUD_CONFIG.bkp
else
    echo Cloud-config original config already backuped as $CLOUD_CONFIG.bkp
fi


# create and install a new cloud-config file from the template
echo Creating a new cloud-config file in $CLOUD_CONFIG...
TEMPFILE=`mktemp`
trap 'rm -f $TEMPFILE' 0 1 2 3 15
(
    echo 'cat <<END_OF_TEXT'
    cat user_data.template
    echo 'END_OF_TEXT'
) > $TEMPFILE
. $TEMPFILE > user_data


# apply the new cloud-config file
if coreos-cloudinit -validate --from-file user_data; then
    echo Applying the new config file...
    mv user_data $CLOUD_CONFIG
    chown root: $CLOUD_CONFIG
    chmod 600 /var/lib/coreos-install/user_data
    coreos-cloudinit --from-file user_data
else
    echo Generated cloud-config file is invalid. Please fix it
    echo ':('
    exit 1
fi


# initialize the local disks
if ! btrfs device scan /dev/sdb /dev/sdc; then
    echo Formatting /dev/sdb and /dev/sdc as BTRFS raid0...
    mkfs.btrfs -f /dev/sdb /dev/sdc
    mkdir -p /mnt/local
    mount /dev/sdb /mnt/local
    echo Creating BTRFS subvolume for docker volumes...
    btrfs subvolume create /mnt/local/volumes
    echo Creating BTRFS subvolume for buttervolume snapshots...
    btrfs subvolume create /mnt/local/snapshots
    umount /mnt/local
    rmdir /mnt/local
    echo done
else
    echo BTRFS filesystem already existing in /dev/sdb and /dev/sdc
fi


# Add the SAN volume
if [ ! -e /dev/sdd ]; then
    echo Adding the RPN-SAN to the system...
    IQN=$(iscsiadm -m discovery -t sendtargets -p  $san_address | awk '{print $2}')
    iscsiadm -m node -T $IQN --login
    systemctl enable iscsid
    systemctl start iscsid
    #mkdir -p /mnt/san
    #mount /dev/sdd /mnt/san
    #btrfs subvolume create /mnt/san/nextcloud
    #umount /mnt/san
else
    echo RPN-SAN is already available
fi


# install docker-compose
if ! which docker-compose; then
    mkdir -p /opt/bin
    curl -L `curl -s https://api.github.com/repos/docker/compose/releases/1.11.2 | jq -r '.assets[].browser_download_url | select(contains("Linux") and contains("x86_64"))'` > /opt/bin/docker-compose
    chmod +x /opt/bin/docker-compose
else
    echo docker-compose already installed
fi


# enable docker
systemctl enable docker

echo ':)'
