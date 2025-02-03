# test/scratchpad work for BigQueryTest
# placeholder function
def FunctionCall(X:int) -> float: return (1/X);

# list of positive inputs/results
results_pos = [
    f"+{X}: {FunctionCall(X):.2f}%"
    for X in range(100, 1000, 25)
]
# list of negative inputs/results
results_neg = [
    f"-{X}: {FunctionCall(-X):.2f}%"
    for X in range(100, 1000, 25)
]

# creating both lists in a single comprehension
results = [x for x in zip(*[
    (f"+{X}: {FunctionCall(X):.2f}%", f"-{X}: {FunctionCall(-X):.2f}%")
    for X in range(100, 1000, 25)
])]

# verifying
assert (results[0] == results_pos)
assert (results[1] == results_neg)

#########################################

list_of_dicts = [
    {"a": 1, "b": 2},
    {"a": -2, "b": -3},
    {"a": 5, "b": 6},
]

dict_of_lists = {
    key: [d2[key] for d2 in list_of_dicts]
    for entry in list_of_dicts
    for key in entry.keys()
}


##############
bookmakers = [
    {
        "key": "draftkings",
        "title": "DraftKings",
        "last_update": "2021-10-18T11:48:09Z",
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {
                        "name": "Buffalo Bills",
                        "price": -294
                    },
                    {
                        "name": "Tennessee Titans",
                        "price": 230
                    }
                ]
            }
        ]
    },
    {
        "key": "twinspires",
        "title": "TwinSpires",
        "last_update": "2021-10-18T11:48:00Z",
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {
                        "name": "Buffalo Bills",
                        "price": -278
                    },
                    {
                        "name": "Tennessee Titans",
                        "price": 220
                    }
                ]
            }
        ]
    },
    {
        "key": "betfair",
        "title": "Betfair",
        "last_update": "2021-10-18T11:48:25Z",
        "markets": [
            {
                "key": "h2h_lay",
                "outcomes": [
                    {
                        "name": "Buffalo Bills",
                        "price": -233
                    },
                    {
                        "name": "Tennessee Titans",
                        "price": 240
                    }
                ]
            },
            {
                "key": "h2h",
                "outcomes": [
                    {
                        "name": "Buffalo Bills",
                        "price": -238
                    },
                    {
                        "name": "Tennessee Titans",
                        "price": 230
                    }
                ]
            }
        ]
    },
    {
        "key": "sugarhouse",
        "title": "SugarHouse",
        "last_update": "2021-10-18T11:48:27Z",
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {
                        "name": "Buffalo Bills",
                        "price": -263
                    },
                    {
                        "name": "Tennessee Titans",
                        "price": 228
                    }
                ]
            }
        ]
    },
    {
        "key": "betrivers",
        "title": "BetRivers",
        "last_update": "2021-10-18T11:45:46Z",
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {
                        "name": "Buffalo Bills",
                        "price": -263
                    },
                    {
                        "name": "Tennessee Titans",
                        "price": 228
                    }
                ]
            }
        ]
    },
    {
        "key": "barstool",
        "title": "Barstool Sportsbook",
        "last_update": "2021-10-18T11:48:21Z",
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {
                        "name": "Buffalo Bills",
                        "price": -278
                    },
                    {
                        "name": "Tennessee Titans",
                        "price": 220
                    }
                ]
            }
        ]
    },
    {
        "key": "fanduel",
        "title": "FanDuel",
        "last_update": "2021-10-18T11:47:58Z",
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {
                        "name": "Buffalo Bills",
                        "price": -270
                    },
                    {
                        "name": "Tennessee Titans",
                        "price": 220
                    }
                ]
            }
        ]
    },
    {
        "key": "betmgm",
        "title": "BetMGM",
        "last_update": "2021-10-18T11:44:23Z",
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {
                        "name": "Buffalo Bills",
                        "price": -250
                    },
                    {
                        "name": "Tennessee Titans",
                        "price": 210
                    }
                ]
            }
        ]
    },
    {
        "key": "unibet",
        "title": "Unibet",
        "last_update": "2021-10-18T11:49:57Z",
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {
                        "name": "Buffalo Bills",
                        "price": -263
                    },
                    {
                        "name": "Tennessee Titans",
                        "price": 225
                    }
                ]
            }
        ]
    },
    {
        "key": "williamhill_us",
        "title": "William Hill (US)",
        "last_update": "2021-10-18T11:48:21Z",
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {
                        "name": "Buffalo Bills",
                        "price": -270
                    },
                    {
                        "name": "Tennessee Titans",
                        "price": 220
                    }
                ]
            }
        ]
    },
    {
        "key": "betonlineag",
        "title": "BetOnline.ag",
        "last_update": "2021-10-18T11:48:28Z",
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {
                        "name": "Buffalo Bills",
                        "price": -256
                    },
                    {
                        "name": "Tennessee Titans",
                        "price": 215
                    }
                ]
            }
        ]
    },
    {
        "key": "pointsbetus",
        "title": "PointsBet (US)",
        "last_update": "2021-10-18T11:48:46Z",
        "markets": [
            {
                "key": "h2h",
                "outcomes": [
                    {
                        "name": "Buffalo Bills",
                        "price": -263
                    },
                    {
                        "name": "Tennessee Titans",
                        "price": 210
                    }
                ]
            }
        ]
    },
]

market_map = [{
    "bookmaker": bm['title'],
    **bm['markets'][0] # 'markets' is always a list[dict] of length 1?
} for bm in bookmakers]

market_map_byteam = [
    {
        'name': outcome['name'],
        'bookmaker': market_dict['bookmaker'],
        'price': outcome['price'],
    }
    for market_dict in market_map
    for outcome in market_dict['outcomes']
]


# market_map_byteamz = {
#     outcome['name']: {
#         'name': outcome['name'],
#         'bookmaker': market_dict['bookmaker'],
#         'price': outcome['price'],
#     }
#     for market_dict in market_map
#     for outcome in market_dict['outcomes']
# }

outcome_map = {
    entry['name']: {
        key: [d2[key] for d2 in market_map_byteam if d2['name'] == entry['name']]
        for key in entry.keys()
    }
    for entry in market_map_byteam
    for key in entry.keys()
}

