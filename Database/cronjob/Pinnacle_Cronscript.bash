#!/bin/bash
# cronjob script 

#set -x

DB_WORKINGDIR=~/Desktop/EffortScraper/Database
DB_CRONJOB_FOLDER="${DB_WORKINGDIR}/cronjob"

source ~/Desktop/EffortScraper/venv/bin/activate
cd ${DB_WORKINGDIR} || exit

# logging scrape
date >> ${DB_CRONJOB_FOLDER}/pinnacledb_cronjob.log
date >> ${DB_CRONJOB_FOLDER}/pinnacledb_output.log
# "&>>" redirects both stdout and stderr
#python3 ~/Desktop/EffortScraper/Database/PinnacleDB.py |& echo >> ~/Desktop/EffortScraper/Database/pinnacledb_output.log
python3 ~/Desktop/EffortScraper/Database/PinnacleDB.py
SCRAPE_EXIT_STATUS=$?
echo "PinnacleDB scrape finished with exit status: ${SCRAPE_EXIT_STATUS}" >> ${DB_CRONJOB_FOLDER}/pinnacledb_cronjob.log

deactivate
exit
