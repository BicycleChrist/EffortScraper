#!/bin/bash
# requires xdotool

zoom() 
{
    KEYBIND="Control_R+plus"
    if [ "${1}" = "-" ]; then
        KEYBIND="Control_R+minus"
        shift 1
    elif [ "${1}" = "+" ]; then
        shift 1
    fi
    
    RESULTARR=($(xdotool search --maxdepth 2 --class firefox))
    FIREFOX_WINDOW_ID=${RESULTARR[-1]}
    ITERATIONS=${1}
    while (( ${ITERATIONS} > 0 )); do
        xdotool key --window "${FIREFOX_WINDOW_ID}" --delay 50 "${KEYBIND}"
        ITERATIONS=$(( ${ITERATIONS} - 1 ))
    done
}

NUMARGS=$#
if (( NUMARGS > 0 )); then
    zoom ${@}
else
    echo "usage:  zoom [+|-] [integer]"
    echo "if no sign is passed, assumed positive"
fi
