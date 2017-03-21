cloud-config
============

Commande et install d'un nouveau serveur :
------------------------------------------

- S'il s'agit d'une réinstall complète, reformatter d'abord /dev/sdb et /dev/sdc
- Sur la console online, pendant la configuration initiale créer un user : mlf
- Ajouter le serveur au groupe RPN dans la console online et attendre 10min
- Démarrer le serveur et se connecter comme mlf, puis ::

    ssh-keygen
    # ajouter la clépublique au gitlab, puis:
    git clone ssh://git@git.mlfmonde.org:2222/hebergement/cluster.git
    cd cluster/cloud-config
    sudo ./install.sh  # ce script peut être lancé plusieurs fois
    # la derniere étape pourle SAN échoue, il faut kill iscsid et réessayer
    reboot


- Se connecter de nouveau comme mlf puis démarrer buttervolume puis le reste::

    cd cluster/buttervolume
    docker-compose up -d
    cd ..
    docker-compose up -d


Checklist après install
-----------------------

- le nouveau cloud-config est en place::

    sudo more /var/lib/coreos-install/user_data

- les 2 interfaces réseau sont bien activées et ont chacune leur IP::

    ip address

- Dans /var/lib/docker/, volumes et snapshots sont des points de montage vers un volume btrfs::

    mount

- le SAN est présent sur /dev/sdd mais non monté::

    cat /proc/partitions
    mount

- docker est activé et démarré::

    systemctl status docker

- docker-compose est installé::

    docker-compose -h

- les 4 services sont en fonctionnement::

    docker ps
