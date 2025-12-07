#!/bin/bash

COMMAND="${@}"
if [[ -z "${@}" ]];
    then echo "no command given - launching shell"; COMMAND="/bin/bash";
    else echo "command args (${#}): ${COMMAND}";
fi;

docker start wooptyROCM
docker exec  --interactive --tty --user 1000 --workdir /NHLvacuum wooptyROCM ${COMMAND}
echo "docker process finished";
