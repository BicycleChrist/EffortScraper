import pprint
from ProbablePitchers import FetchProbablePitchers, ParseProbablePitchers
from bigBOVpoolskiMLB import Main as scrape_bigBOVpoolski_data
#TODO: update import paths to just use bigBOVpoolski
# Might be tough to format the output to be poperly used as it is now

def get_pitcher_names_from_matchup_dict(matchup_dict):
    pitcher_names = []
    for matchup in matchup_dict["matchups"]:
        pitcher_names.extend(matchup["pitchers"].keys())
    return pitcher_names

def get_relevant_bigBOVpoolski_data(scraped_data, pitcher_names):
    relevant_data = {}
    for url, data in scraped_data.items():
        for data_dict in data:
            header = data_dict['header'][0]
            section = data_dict['section']
            for pitcher_name in pitcher_names:
                if pitcher_name in header:
                    if pitcher_name not in relevant_data:
                        relevant_data[pitcher_name] = []
                    relevant_data[pitcher_name].append({header: section})
    return relevant_data

def main():

    soup = FetchProbablePitchers()
    if not soup:
        print("Failed to fetch probable pitchers data.")
        return
    matchup_dict = ParseProbablePitchers(soup)
    pitcher_names = get_pitcher_names_from_matchup_dict(matchup_dict)
    scraped_bigBOVpoolski_data = scrape_bigBOVpoolski_data()
    relevant_data = get_relevant_bigBOVpoolski_data(scraped_bigBOVpoolski_data, pitcher_names)
    pprint.pprint(relevant_data)

if __name__ == "__main__":
    main()
