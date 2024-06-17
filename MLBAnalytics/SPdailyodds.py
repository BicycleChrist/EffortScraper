import pprint
from ProbablePitchers import FetchProbablePitchers, ParseProbablePitchers
from Boddssuck.bigBOVpoolski import Main as scrape_bigBOVpoolski_data
import re

# parse Over/Under betting odds from a given section
def parse_over_under_odds(section):
    odds = re.findall(r'EVEN|[+-]\d+', section)
    line_match = re.search(r'(\d+(\.\d+)?)', section)
    line = line_match.group(0) if line_match else None
    print(f"Parsing Over/Under odds from section: {section}")  
    print(f"Found line: {line}, Found Over/Under odds: {odds}")  
    if len(odds) == 2:
        over = odds[0]
        under = odds[1]
        return line, over, under
    return line, None, None

# parse Yes/No betting odds from a given section
def parse_yes_no_odds(section):
    print(f"Parsing Yes/No odds from section: {section}")  
    section_split = section.split()
    try:
        yes_index = section_split.index('Yes')
        no_index = section_split.index('No')
        yes_odds = section_split[yes_index + 1]
        no_odds = section_split[no_index + 1]
        print(f"Found Yes odds: {yes_odds}, No odds: {no_odds}")  # Debug print
        return yes_odds, no_odds
    except ValueError as e:
        print(f"Error parsing Yes/No odds: {e}")
        return None, None

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
            section_list = data_dict['section']
            section = ' '.join(section_list) if isinstance(section_list, list) else section_list
            for pitcher_name in pitcher_names:
                if pitcher_name in header:
                    if pitcher_name not in relevant_data:
                        relevant_data[pitcher_name] = []
                    # Check if it's a Yes/No market
                    if 'Yes' in section and 'No' in section:
                        yes_odds, no_odds = parse_yes_no_odds(section)
                        if yes_odds and no_odds:
                            relevant_data[pitcher_name].append({
                                'header': header,
                                'yes': yes_odds,
                                'no': no_odds
                            })
                            print(f"Added Yes/No market for {pitcher_name}: {header}, Yes: {yes_odds}, No: {no_odds}")  # Debug print
                    else:
                        line, over, under = parse_over_under_odds(section)
                        if over and under:
                            relevant_data[pitcher_name].append({
                                'header': header,
                                'line': line,
                                'over': over,
                                'under': under
                            })
                            print(f"Added Over/Under market for {pitcher_name}: {header}, Line: {line}, Over: {over}, Under: {under}")  # Debug print
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
    
   
    for pitcher, data in relevant_data.items():
        print(f"Pitcher: {pitcher}")
        for item in data:
            print(f"  Market: {item['header']}")
            if 'line' in item:
                print(f"    Line: {item['line']}")
            if 'over' in item and 'under' in item:
                print(f"    Over: {item['over']}")
                print(f"    Under: {item['under']}")
            elif 'yes' in item and 'no' in item:
                print(f"    Yes: {item['yes']}")
                print(f"    No: {item['no']}")
        print()

if __name__ == "__main__":
    main()
