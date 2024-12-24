from pmapicall import *
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import as_completed

# TODO: option for old multi-window display instead of subplot
# label is the text to put in the legend
def plot_price_history(index:int, label, timeseries, market_title="Market Price History", total_plots=0):
    """
    Plot price history from a timeseries returned by ConstructTimeseries
    
    Parameters:
    index (int): Index of the current plot
    label (str): Label for the legend
    timeseries (list): List of dicts with 'price', 'timestamp', and 'date' keys
    market_title (str): Title for the plot
    total_plots (int): Total number of plots to be created
    """
    # Convert to pandas DataFrame for easier handling
    df = pd.DataFrame(timeseries)
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='s')
    
    # Calculate optimal grid layout based on total plots
    if total_plots == 0:
        total_plots = 1
    cols = min(4, total_plots)  # Maximum 4 columns
    rows = (total_plots + cols - 1) // cols  # Floor division for rows
    
    # Create or get existing figure
    if index == 0:
        plt.figure(figsize=(15, 3 * rows))  # Adjust figure size based on number of rows
    
    # Create subplot with proper layout
    plt.subplot(rows, cols, index + 1)
    
    # Plot the price history
    # plt.plot(df['datetime'], df['price'], linewidth=2, marker='D', label=label)
    plt.plot(df['datetime'], df['price'], linewidth=2, label=label)
    
    # Customize the plot
    plt.title(market_title, fontsize=10, pad=2)
    plt.xlabel('',fontsize=2) # Date
    # plt.ylabel('Price', fontsize=6)
    
    ax = plt.gca()
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.tick_params(axis='x', labelsize=6)
    
    # Add grid
    plt.grid(True, linestyle='--', alpha=0.7)
    # plt.xticks()
    
    # Calculate price statistics
    min_price = df['price'].min()
    max_price = df['price'].max()
    current_price = df['price'].iloc[-1]
    
    # Add text box with price information - adjusted position
    info_text = f'Current: {current_price:.3f}\nMin: {min_price:.3f}\nMax: {max_price:.3f}'
    props = dict(boxstyle='round', facecolor='white', alpha=0.8)
    plt.text(0.02, 0.98, info_text,
             transform=plt.gca().transAxes,
             fontsize=7,
             verticalalignment='top',
             bbox=props)
    
    # Add legend with adjusted position
    plt.legend(loc='upper right', bbox_to_anchor=(1, 1),
              fancybox=True, shadow=True, fontsize=6)
    
    # Adjust subplot parameters if this is the last plot
    if index == total_plots - 1:
        plt.gcf().tight_layout()
        # Add extra space between subplots
        plt.subplots_adjust(hspace=0.4, wspace=0.3)
    
    return plt.gcf()


# query is text that will be used to filter markets based on question
def PlotMarkets(query:str, markets, single_graph=False, confirm=False):
    matching_markets = [market for market in markets if query in market['question']]
    print(f"found {len(matching_markets)} markets matching '{query}'")
    
    if confirm:
        userinput = input("continue? (Y/N, List): ")
        if userinput.capitalize() == 'List':
            for market in matching_markets: print(market['question'])
            userinput = input("\ncontinue? (Y/N): ")
        if userinput.capitalize() != 'Y': print("cancelling"); return
    
    # Close any existing plots
    plt.close('all')
    
    # Calculate total number of plots
    

    for (index, market) in enumerate(matching_markets):
        timeseries_pair = [GetPriceHistory(int(token_id), fidelity_hours=12) for token_id in market['token_ids']]
        line_labels = [line.split(':', maxsplit=1)[0] for line in market['lines']]
        for (label,timeseries) in zip(line_labels, timeseries_pair):
            title = market['question']
            if single_graph: 
                index = 0
                label = title + ": " + label
                title = query
            plot_price_history(index, label, timeseries, title, len(matching_markets))
    
    plt.show(block=True)
    print("done")
    return


# for multithreaded version. Bugged; fails to fetch timeseries for most markets
def fetch_price_histories(market) -> tuple:
    """Helper function to fetch price histories for a single market"""
    timeseries_pair = [GetPriceHistory(int(token_id), fidelity_hours=12) 
                      for token_id in market['token_ids']]
    line_labels = [line.split(':', maxsplit=1)[0] for line in market['lines']]
    return (market['question'], line_labels, timeseries_pair)

# multithreaded version, presntly broken. Need to find request limit in docs. 
# consistent 429 resppnse code
def PlotMarketsMultiThreaded(query:str, markets, single_graph=False, market_limit=500, max_workers=10):
    matching_markets = [market for market in markets if query in market['question']][:market_limit]
    print(f"Processing {len(matching_markets)} markets out of {len([m for m in markets if query in m['question']])} matches")
    
    userinput = input("continue? (Y/N, List): ")
    if userinput.capitalize() == 'List':
        for market in matching_markets: print(market['question'])
        userinput = input("\ncontinue? (Y/N): ")
    if userinput.capitalize() != 'Y': print("cancelling"); return

    plt.close('all')
    
    def process_market(market_data):
        index, market = market_data
        try:
            timeseries_pair = [GetPriceHistory(int(token_id), fidelity_hours=12) 
                             for token_id in market['token_ids']]
            line_labels = [line.split(':', maxsplit=1)[0] for line in market['lines']]
            title = market['question']
            
            for label, timeseries in zip(line_labels, timeseries_pair):
                plot_title = title if not single_graph else query
                plot_index = 0 if single_graph else index
                plot_label = f"{title}: {label}" if single_graph else label
                plot_price_history(plot_index, plot_label, timeseries, plot_title)
                
        except Exception as e:
            print(f"Error processing market {index}: {str(e)}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        executor.map(process_market, enumerate(matching_markets))
    
    plt.show(block=True)
    print("done")


if __name__ == "__main__":
    markets = LoadJsonDump()
    #PlotMarkets('OpenSea', markets)
    PlotMarkets('NFL MVP', markets, False)
    #PlotMarketsMultiThreaded("Trump", markets, market_limit=1000, max_workers=16)
