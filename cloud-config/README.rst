cloud-config
============

Commande et install d'un nouveau serveur :

- Sur la console online, pendant la configuration initiale créer un user : mlf
- Ajouter le serveur au groupe RPN dans la console online et attendre 10min
- Démarrer le serveur et se connecter comme mlf, puis ::
    ssh-keygen
    # ajouter la clépublique au gitlab, puis:
    git clone ssh://git@git.mlfmonde.org:2222/hebergement/cluster.git
    cd cluster/cloud-config
    sudo ./install.sh
    reboot
- Se connecter de nouveau comme mlf puis ::
    cd cluster
    docker-compose up -d
