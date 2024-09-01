from pprint import pprint
from os import system

def ParseHrefs(market_links: list[str]):
    mapping = {
        "": [] # entries without any subentries
    }
    market_links = [href.removeprefix("https://polymarket.com/event/") for href in market_links]
    while len(market_links) > 0:
        section = market_links.pop(0) # assuming that the first one is a section title, and that all links are preceeded by their section title
        section_entries = [link.removeprefix(section+'/') for link in market_links if link.startswith(section)]
        if len(section_entries) == 0: mapping[""].append(section); continue;
        mapping[section] = section_entries
        market_links = market_links[len(section_entries):]
    return mapping


# calling bash script because Selenium sucks lmao
def ZoomOutFirefox(increments=20):
    exit_status = os.system(f"./zoom.bash - {increments}")
    return exit_status


if __name__ == "__main__":
    from Polymarket_testcases import href_testinput
    original_count = len(href_testinput)
    parsed = ParseHrefs(href_testinput)
    final_count = len(parsed.keys()) - 1  # since the 'empty' section always exists
    for entrylist in parsed.values(): final_count += len(entrylist)
    if not original_count == final_count:
        print(f"error: counts don't match original_count: {original_count} final_count: {final_count}")
        print(href_testinput)
        exit(1)
    
    pprint(parsed)
