import requests
import pprint

def GetRobots(target):
    response = requests.get(f'https://www.{target}/robots.txt')
    # response is a bitstream
    decoded = response.content.decode()  # convert to string
    lines = decoded.split('\n')
    pprint.pprint(lines)
    return lines


if __name__ == "__main__":
    targetsite = "collegehockeynews.com"
    GetRobots(targetsite)