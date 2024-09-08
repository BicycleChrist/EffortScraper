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
    
    RESULTARR=($(xdotool search --class firefox))
    
    PROCESS_COUNT=${#RESULTARR[*]}
    if (( $PROCESS_COUNT < 1 )); then
        echo "could not identify any firefox processes"
        return 3
    fi
    
    FIREFOX_WINDOW_ID=${RESULTARR[-1]}
    ITERATIONS=${1}
    while (( ${ITERATIONS} > 0 )); do
        xdotool key --window "${FIREFOX_WINDOW_ID}" --delay 50 "${KEYBIND}"
        ITERATIONS=$(( ${ITERATIONS} - 1 ))
    done
}

IsolateFirefoxWindowID()
{
    VISIBLE_WINDOW_ID=$(xdotool search --onlyvisible --limit 1 --class "firefox")
    if (( $? != 0 )); then
        echo "could not identify any visible firefox windows"
        echo "click on a window to print the ID"
        xdotool selectwindow
    fi
    echo "visible window ID: ${VISIBLE_WINDOW_ID}"
    
    RESULTARR=($(xdotool search --class firefox))
    if (( $? != 0 )); then
        echo "could not identify any firefox processes"
        return 2
    fi
    
    PROCESS_COUNT=${#RESULTARR[*]}
    if (( $PROCESS_COUNT < 1 )); then
        echo "could not identify any firefox processes"
        return 3
    fi
    
    echo "found ${PROCESS_COUNT} processes: ${RESULTARR[*]}"
    FIREFOX_WINDOW_ID=${RESULTARR[1]}
    
    #for (( ID in ${RESULTARR[*]} ));
    ITERATIONS=0
    while (( ${ITERATIONS} < ${PROCESS_COUNT})); do
        FIREFOX_WINDOW_ID=${RESULTARR[$ITERATIONS]}
        echo -n "checking ${FIREFOX_WINDOW_ID}... "
        xdotool windowactivate "${FIREFOX_WINDOW_ID}"
        # for some reason the return status is always zero
        ITERATIONS=$(( ${ITERATIONS} + 1 ))
    done
    
    echo ""
    
    return 0
}

NUMARGS=$#
if (( NUMARGS > 0 )); then
    zoom ${@}
else
    echo "usage:  zoom [+|-] [integer]"
    echo "        if no sign is passed, assumed positive"
    echo "IsolateFirefoxWindowID: search for firefox processes and test each one"
fi
