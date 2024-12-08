from pmapicall import *
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime
import pandas as pd

# label is the text to put in the legend
def plot_price_history(index:int, label, timeseries, market_title="Market Price History"):
    """
    Plot price history from a timeseries returned by ConstructTimeseries
    
    Parameters:
    timeseries (list): List of dicts with 'price', 'timestamp', and 'date' keys
    market_title (str): Title for the plot
    """
    # Convert to pandas DataFrame for easier handling
    df = pd.DataFrame(timeseries)
    
    # Convert timestamp to datetime for better x-axis formatting
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='s')
    
    # Create the figure and axis
    plt.figure(index, figsize=(12, 6))
    
    # Plot the price history
    #plt.plot(df['datetime'], df['price'], linewidth=2, color=line_color)
    plt.plot(df['datetime'], df['price'], linewidth=2, label=label)
    
    # Customize the plot
    plt.title(market_title, fontsize=14, pad=20)
    plt.xlabel('Date', fontsize=12)
    plt.ylabel('Price', fontsize=12)
    
    # Format x-axis
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
    plt.gca().xaxis.set_major_locator(mdates.AutoDateLocator())
    
    # Add grid
    plt.grid(True, linestyle='--', alpha=0.7)
    
    # Rotate and align the tick labels so they look better
    plt.gcf().autofmt_xdate()
    
    # Add price range info
    min_price = df['price'].min()
    max_price = df['price'].max()
    current_price = df['price'].iloc[-1]
    
    # Add text box with price information
    info_text = f'Current: {current_price:.3f}\nMin: {min_price:.3f}\nMax: {max_price:.3f}'
    # plt.legend(bbox_to_anchor=(0, 1.02, 1, 0.2), 
    #       loc="lower left",
    #       mode="expand", 
    #       borderaxespad=0,
    #       ncol=3,
    #       fancybox=True, 
    #       shadow=True)
    
    # Or alternatively, you can create a text box instead of a legend:
    props = dict(boxstyle='round', facecolor='white', alpha=0.8)
    plt.text(0.02, 0.98, 
             info_text,
             transform=plt.gca().transAxes,
             fontsize=9,
             verticalalignment='top',
             bbox=props)
    
    # To ensure there's enough room at the top:
    plt.subplots_adjust(top=0.85)  # Adjust this value as needed
    plt.legend(loc='upper right', bbox_to_anchor=(1, 1),
              fancybox=True, shadow=True)
    
    # Adjust layout to prevent label cutoff
    plt.tight_layout()
    return plt.gcf()


# query is text that will be used to filter markets based on question
def PlotMarkets(query:str, markets, single_graph=False):
    matching_markets = [market for market in markets if query in market['question']]
    print(f"found {len(matching_markets)} markets matching '{query}'")
    userinput = input("continue? (Y/N, List): ")
    if userinput.capitalize() == 'List':
        for market in matching_markets: print(market['question']);
        userinput = input("\ncontinue? (Y/N): ")
    if userinput.capitalize() != 'Y': print("cancelling"); return;
    for (index,market) in enumerate(matching_markets):
        timeseries_pair = [GetPriceHistory(int(token_id), fidelity_hours=12) for token_id in market['token_ids']]
        line_labels = [line.split(':', maxsplit=1)[0] for line in market['lines']]
        for (label,timeseries) in zip(line_labels, timeseries_pair):
            title = market['question']
            if single_graph: 
                index = 0
                label = title + ": " + label
                title = query
            plot_price_history(index, label, timeseries, title)
        #plt.show(block=False)
    plt.show(block=True)
    print("done")
    return


if __name__ == "__main__":
    markets = LoadJsonDump()
    #PlotMarkets('OpenSea', markets)
    PlotMarkets('NFL MVP', markets, False)
