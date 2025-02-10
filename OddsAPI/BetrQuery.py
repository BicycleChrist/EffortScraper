import pathlib
import requests
import json
from datetime import *
from Creds import BETR_AUTH_TOKEN


def JakePaulAPI(operationName:str, variables:dict = None):
    query = ""
    local_variables_dict = {}
    missing_vars = []

    # seems that you need to provide the entire graphql class/structure definition in the request?
    # TODO: match statement
    if operationName == "TopTenPlayersData":
        query = "query TopTenPlayersData {\n  getTopTenPlayersData {\n    ...EventInfoData\n    ... on TeamTournamentEvent {\n      teams {\n        ...TeamInfoWithPlayers\n        __typename\n      }\n      __typename\n    }\n    ... on TeamVersusEvent {\n      teams {\n        ...TeamInfoWithPlayers\n        __typename\n      }\n      __typename\n    }\n    ... on IndividualTournamentEvent {\n      players {\n        ...PlayerInfoWithProjections\n        __typename\n      }\n      __typename\n    }\n    ... on IndividualVersusEvent {\n      players {\n        ...PlayerInfoWithProjections\n        __typename\n      }\n      __typename\n    }\n    __typename\n  }\n}\nfragment EventInfoData on EventV2 {\n  id\n  date\n  status\n  sport\n  league\n  competitionType\n  dataFeedSourceIds {\n    id\n    source\n    __typename\n  }\n  playerStructure\n  venueDetails {\n    name\n    city\n    country\n    __typename\n  }\n  headerImage\n  attributes {\n    key\n    value\n    __typename\n  }\n  name\n  icon\n  dedicated\n  __typename\n}\nfragment TeamInfoWithPlayers on Team {\n  ...TeamInfo\n  players {\n    ...PlayerInfoWithProjections\n    __typename\n  }\n  __typename\n}\nfragment TeamInfo on Team {\n  id\n  name\n  league\n  sport\n  icon\n  color\n  secondaryColor\n  largeIcon\n  __typename\n}\nfragment PlayerInfoWithProjections on Player {\n  ...PlayerInfo\n  projections {\n    ...PlayerProjection\n    __typename\n  }\n  __typename\n}\nfragment PlayerInfo on Player {\n  id\n  firstName\n  lastName\n  icon\n  position\n  jerseyNumber\n  attributes {\n    key\n    value\n    __typename\n  }\n  __typename\n}\nfragment PlayerProjection on Projection {\n  marketId\n  marketStatus\n  isLive\n  type\n  label\n  name\n  key\n  order\n  value\n  nonRegularPercentage\n  nonRegularValue\n  allowedOptions {\n    marketOptionId\n    outcome\n    __typename\n  }\n  currentValue\n  __typename\n}"
    elif operationName == "AllLeaguesUpcomingEvents":
        query = "query AllLeaguesUpcomingEvents {\n  getUpcomingEventsV2 {\n    id\n    league\n    __typename\n  }\n}"
    elif operationName == "LobbyTrendingEvents":
        query = "query LobbyTrendingEvents {  getUpcomingLobbyEventsV2 {    ...EventInfoData    ... on TeamTournamentEvent {      teams {        ...TeamInfoWithPlayers        __typename      }      __typename    }    ... on TeamVersusEvent {      teams {        ...TeamInfoWithPlayers        __typename      }      __typename    }    ... on IndividualTournamentEvent {      players {        ...PlayerInfoWithProjections        __typename      }      __typename    }    ... on IndividualVersusEvent {      players {        ...PlayerInfoWithProjections        __typename      }      __typename    }    dedicated    __typename  }}fragment EventInfoData on EventV2 {  id  date  status  sport  league  competitionType  dataFeedSourceIds {    id    source    __typename  }  playerStructure  venueDetails {    name    city    country    __typename  }  headerImage  attributes {    key    value    __typename  }  name  icon  dedicated  __typename}fragment TeamInfoWithPlayers on Team {  ...TeamInfo  players {    ...PlayerInfoWithProjections    __typename  }  __typename}fragment TeamInfo on Team {  id  name  league  sport  icon  color  secondaryColor  largeIcon  __typename}fragment PlayerInfoWithProjections on Player {  ...PlayerInfo  projections {    ...PlayerProjection    __typename  }  __typename}fragment PlayerInfo on Player {  id  firstName  lastName  icon  position  jerseyNumber  attributes {    key    value    __typename  }  __typename}fragment PlayerProjection on Projection {  marketId  marketStatus  isLive  type  label  name  key  order  value  nonRegularPercentage  nonRegularValue  allowedOptions {    marketOptionId    outcome    __typename  }  currentValue  __typename}"
    elif operationName == "EventInfoWithPlayers":
        query = "query EventInfoWithPlayers($id: String!) {  getEventByIdV2(id: $id) {    ...EventInfoData    ... on TeamTournamentEvent {      teams {        ...TeamInfoWithPlayers        __typename      }      __typename    }    ... on TeamVersusEvent {      teams {        ...TeamInfoWithPlayers        __typename      }      __typename    }    ... on IndividualTournamentEvent {      players {        ...PlayerInfoWithProjections        __typename      }      __typename    }    ... on IndividualVersusEvent {      players {        ...PlayerInfoWithProjections        __typename      }      __typename    }    __typename  }}fragment EventInfoData on EventV2 {  id  date  status  sport  league  competitionType  dataFeedSourceIds {    id    source    __typename  }  playerStructure  venueDetails {    name    city    country    __typename  }  headerImage  attributes {    key    value    __typename  }  name  icon  dedicated  __typename}fragment TeamInfoWithPlayers on Team {  ...TeamInfo  players {    ...PlayerInfoWithProjections    __typename  }  __typename}fragment TeamInfo on Team {  id  name  league  sport  icon  color  secondaryColor  largeIcon  __typename}fragment PlayerInfoWithProjections on Player {  ...PlayerInfo  projections {    ...PlayerProjection    __typename  }  __typename}fragment PlayerInfo on Player {  id  firstName  lastName  icon  position  jerseyNumber  attributes {    key    value    __typename  }  __typename}fragment PlayerProjection on Projection {  marketId  marketStatus  isLive  type  label  name  key  order  value  nonRegularPercentage  nonRegularValue  allowedOptions {    marketOptionId    outcome    __typename  }  currentValue  __typename}"
        if variables is None: print(f"ERROR: {operationName} requires variable: 'id'"); return None,None;
        # TODO: actually handle the variables dict
        local_variables_dict['id'] = variables['id']
    elif operationName == "UpcomingEventsConditionalWithPlayers":
        req_vars = ["league", "withPlayers"]
        if variables is None: missing_vars = req_vars;
        else: missing_vars = [reqvar for reqvar in req_vars if reqvar not in variables.keys()];
        if (len(missing_vars) > 0): print(f"ERROR: {operationName} requires variables: {missing_vars}"); return None,None;
        query = "query UpcomingEventsConditionalWithPlayers($league: League!, $withPlayers: Boolean!) {  getUpcomingEventsV2(league: $league) {    ...EventInfoData    ... on TeamTournamentEvent {      teams {        ...TeamInfo        __typename      }      __typename    }    ... on TeamVersusEvent {      teams {        ...TeamInfo        __typename      }      __typename    }    ... on IndividualTournamentEvent {      players @include(if: $withPlayers) {        ...PlayerInfoWithProjections        __typename      }      __typename    }    ... on IndividualVersusEvent {      players @include(if: $withPlayers) {        ...PlayerInfoWithProjections        __typename      }      __typename    }    __typename  }}fragment EventInfoData on EventV2 {  id  date  status  sport  league  competitionType  dataFeedSourceIds {    id    source    __typename  }  playerStructure  venueDetails {    name    city    country    __typename  }  headerImage  attributes {    key    value    __typename  }  name  icon  dedicated  __typename}fragment TeamInfo on Team {  id  name  league  sport  icon  color  secondaryColor  largeIcon  __typename}fragment PlayerInfoWithProjections on Player {  ...PlayerInfo  projections {    ...PlayerProjection    __typename  }  __typename}fragment PlayerInfo on Player {  id  firstName  lastName  icon  position  jerseyNumber  attributes {    key    value    __typename  }  __typename}fragment PlayerProjection on Projection {  marketId  marketStatus  isLive  type  label  name  key  order  value  nonRegularPercentage  nonRegularValue  allowedOptions {    marketOptionId    outcome    __typename  }  currentValue  __typename}"
        local_variables_dict = variables
    else:
        print(f"ERROR: unrecognized operation: {operationName}"); return None,None;

    request = {
        "operationName": operationName,
        "query": query,
        "variables": local_variables_dict,
    }

    headers = { "authorization": BETR_AUTH_TOKEN }
    url = "https://api.fantasy.betr.app/graphql"
    response = requests.post(url, headers=headers, json=request)
    content = response.json()
    return (content, response)


def SaveResponse(operationName:str, content:dict):
    cwd = pathlib.Path.cwd()
    savedir = cwd / "betr_dumps"
    if not savedir.exists(): savedir.mkdir()
    dumpfile = savedir / f"{operationName}_{datetime.now().date()}.json"
    print(f"saving to: {dumpfile}")
    json.dump(content, dumpfile.open('w', encoding="utf-8"), indent=2)
    return


def MakeRequests(queries:list[str], vars:dict):
    results = {}
    for query in queries:
        query_vars = vars.get(query, None)
        print(f"querying {query}...")
        if query_vars: print(f"args: {query_vars}");
        (content, response) = JakePaulAPI(query, query_vars)
        if response.status_code != 200: print(f"ERROR: response status {response.status_code}"); continue;
        if not content: print(f"ERROR: no data returned!"); continue;
        results[query] = content
        SaveResponse(query, content)
    print("done")
    return results


if __name__ == "__main__":
    query_args = { "UpcomingEventsConditionalWithPlayers": { "league":"NBA", "withPlayers":False }}
    queries = [
        "TopTenPlayersData",
        "AllLeaguesUpcomingEvents",
        "LobbyTrendingEvents",
        "UpcomingEventsConditionalWithPlayers",
    ]
    results = MakeRequests(queries, query_args)
    SaveResponse("AllResults", results)
    print("finished ripping Jake Paul's API")

