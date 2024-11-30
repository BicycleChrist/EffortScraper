from pmapicall import *
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime
import pandas as pd

def plot_price_history(timeseries, market_title="Market Price History"):
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
    plt.figure(0, figsize=(12, 6))
    
    # Plot the price history
    #plt.plot(df['datetime'], df['price'], linewidth=2, color=line_color)
    plt.plot(df['datetime'], df['price'], linewidth=2)
    
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
             'Current: 0.505\nMin: 0.315\nMax: 0.675',
             transform=plt.gca().transAxes,
             fontsize=9,
             verticalalignment='top',
             bbox=props)
    
    # To ensure there's enough room at the top:
    plt.subplots_adjust(top=0.85)  # Adjust this value as needed
    
    # Adjust layout to prevent label cutoff
    plt.tight_layout()
    return plt.gcf()

def PlotMarket(market:dict):
    timeseries_pair = [GetPriceHistory(int(token_id), fidelity_hours=12) for token_id in market['token_ids']]
    for timeseries in timeseries_pair: plot_price_history(timeseries)
    plt.show(block=True)

if __name__ == "__main__":
    # these token_ids are for the presidential election
    market = "presidential_election_2024"
    token_ids = [11015470973684177829729219287262166995141465048508201953575582100565462316088, 65444287174436666395099524416802980027579283433860283898747701594488689243696]
    timeseries_pair = [GetPriceHistory(token_id, fidelity_hours=12) for token_id in token_ids]
    for timeseries in timeseries_pair: plot_price_history(timeseries)
    plt.show(block=True)
    print("done")
