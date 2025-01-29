
# Get event IDs for a sport
params={
    #"apiKey": API_KEY,
    "sport": "basketball_nba",
}

response = requests.get(
    f"https://api.the-odds-api.com/v4/sports/{params["sport"]}/events",
    params
)

events = response.json()
fields = ["home_team", "away_team"]
matchups = [ { field: event[field] for field in fields } for event in events ]
matchup_strings = [" vs ".join(matchup.values()) for matchup in matchups]

# ----------------------------------------------------------------------------------

# selecting last event for example query
event = events[-1]

# event query
# TODO: get comprehensive list of available markets for event queries
# https://the-odds-api.com/sports-odds-data/betting-markets.html
params = {
    #"apiKey": API_KEY,
    "sport": "basketball_nba",
    "eventId": event['id'],
    "regions": 'us,us2',
    "markets": 'spreads,totals,alternate_spreads,alternate_totals',
}
response = requests.get(
    f"https://api.the-odds-api.com/v4/sports/{params['sport']}/events/{event['id']}/odds",
    params
)
# the most optimal way to make queries would be to search one region/market at a time;
# because the number of regions/markets are multipliers on the cost, regardless of number of results,
# but the query is free if there are no results.
# So querying 10 markets would cost 10 credits even if 9 of those markets have no results,
# whereas 10 individual queries (1 for each market) would have only cost 1 credit (9 empty results are free)

event_odds = response.json()
cost = response.headers['x-requests-last']; print(cost)
remaining = response.headers['x-requests-remaining']
results = event_odds['bookmakers'] # all other returned data is identical to the info in 'event'; this field is the only new data
# results is a list[dict]
