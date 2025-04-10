import requests
import pathlib
import json

from Creds import TT_KEY


def GetEvents(league_id:int, event_type:str):
    assert(event_type in ("upcoming", "inplay"))
    BASE_URL = "https://api.b365api.com"
    SPORT_ID = 92 # table tennis
    print(f"getting events [league_id: {league_id}; event_type: {event_type}]")
    
    params = {
        "token": TT_KEY,
        "sport_id": SPORT_ID,
        "league_id": league_id,
    }
    response = requests.get(f"{BASE_URL}/v3/events/{event_type}", params)
    return response.json()


def GetMarkets(event_id:int, bookmakers:list[str]):
    valid_bookmakers = ("bet365","10bet","ladbrokes","williamhill","betclic","pinnaclesports","planetwin365","ysb88","188bet","unibet","bwin","betfair","betfred","cloudbet","betsson","betdaq","paddypower","sbobet","betathome","dafabet","marathonbet","betvictor","everygame","interwetten","betway","1xbet","nitrogensports","skybet","marsbet","cashpoint","macauslot","hkjc","ggbet","mansion","spreadex","virginbet",)
    assert(bookmakers[0] in valid_bookmakers)
    # TODO: allow multiple bookmakers to be specified (or not because bet365 is the only valid one?)
    
    print(f"fetching markets for event_id: {event_id}")
    params = {
        "token": TT_KEY,
        "event_id": event_id,
        "source": bookmakers[0],
    }
    response = requests.get("https://api.b365api.com/v2/event/odds", params)
    if (response.status_code != 200): print(f"REQUEST FAILED!!!"); print(f"event_id: {event_id}; response: {response.json()}"); exit(1)
    result = response.json()
    if (result["success"] != 1): print(f"unsuccessful request!"); print(f"event_id: {event_id}; response: {response.json()}");
    return result["results"]


def SaveJson(thejson:dict, name):
    savedir = pathlib.Path.cwd()/"TTT_savedata"
    if not savedir.exists(): savedir.mkdir();
    filepath = savedir/f"{name.replace(' ','-')}.json"
    print(f"saving data to: {filepath}")
    with open(filepath, 'w', encoding='utf-8') as thefile:
        json.dump(thejson, thefile, indent=2)
    print("finished writing data")
    return


def Main():
    TARGET_LEAGUE_IDS = {
        22307: "Setka Cup",
        22742: "Czech Republic Liga Pro",
        22534: "TT CUP",
        24536: "Poland TT Elite Series",
    }
    bookmakers = ["bet365"] # default
    bookmaker_keys = [f"{name}_id" for name in bookmakers] # always "bet365_id" - never changes regardless of bookmaker????
    
    upcoming_events = {}
    for (ID, name) in TARGET_LEAGUE_IDS.items():
        event = GetEvents(ID, "upcoming")
        market_entries = []
        for entry in event['results']:
            valid_keys = [bm_key for bm_key in bookmaker_keys if bm_key in entry.keys()]
            if (len(valid_keys) == 0): print(f"no valid bookmakers!"); print(entry); print('\n');
            market_entry = {"event_id": entry['id'], "bookmakers": [entry[key] for key in valid_keys]}
            market_entry['markets'] = GetMarkets(market_entry['event_id'], bookmakers)
            market_entries.append(market_entry)
        event["markets"] = market_entries
        upcoming_events[name] = event
        SaveJson(event, name)
    return upcoming_events


if __name__ == "__main__":
    upcoming_events = Main()
    SaveJson(upcoming_events, "all upcoming events")
