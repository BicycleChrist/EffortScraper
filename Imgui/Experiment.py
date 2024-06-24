import imgui
from ImguiTest import CreateWindow

import update_importpaths
import MLBAnalytics.BBSplayer_ids as BBSplayerIDs 


displayedID = 0

def RenderFrameCustom():
    imgui.new_frame()
    imgui.begin("displayedID")
    global displayedID 
    imgui.text(f"{displayedID}")
    imgui.end()
    
    imgui.begin("BBSplayer_ids")
    allNames = [*BBSplayerIDs.hitters.keys(), *BBSplayerIDs.pitchers.keys()]
    
    for name in allNames:
        if imgui.button(name):
            displayedID = BBSplayerIDs.LookupPlayerID(name, split_on=", ")[1]
    
    imgui.end()
    return


if __name__ == "__main__":
    CreateWindow(RenderFrameCustom)
