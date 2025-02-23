from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QTabWidget, QTabBar, QMainWindow, QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem
from PyQt6.QtGui import QPainter, QPen, QColor

import sys
from PyQt6.QtCore import qDebug

qDebug("Starting debug mode")

class DetachableTabWidget(QTabWidget):
    tabDetached = pyqtSignal(str)  # Add this line to emit the league name

    def __init__(self, data_manager, parent=None):
        super().__init__(parent)
        self.data_manager = data_manager
        self.tabBar = DetachableTabBar(self)
        self.setTabBar(self.tabBar)
        self.tabBar.detachRequested.connect(self.detachTab)
        self.data_manager.odds_updated.connect(self.update_tab_odds)
        self.detached_windows = []
    def detachTab(self, index):
        if index < 0 or index >= self.count():
            return
    
        tab_widget = self.widget(index)  # Get the table widget
        tab_text = self.tabText(index)   # Get tab name
    
        if not tab_widget or not tab_text:
            return
    
        self.removeTab(index)  # Remove it from the main tab widget
    
        # Create a new window for the detached tab
        new_window = QMainWindow()
        new_window.setWindowTitle(tab_text)
    
        # Create a new container widget and set layout
        container = QWidget()
        layout = QVBoxLayout(container)
        
        # Reparent the table widget so it remains persistent
        tab_widget.setParent(None)  
        layout.addWidget(tab_widget)
    
        container.setLayout(layout)
        new_window.setCentralWidget(container)
        
        # Show window
        new_window.show()
    
        # Store reference to prevent garbage collection
        self.detached_windows.append(new_window)



    
    def update_tab_odds(self, odds, league_name):
        """Update the odds in the corresponding tab"""
        for index in range(self.count()):
            if self.tabText(index) == league_name:
                tab_widget = self.widget(index)
                if hasattr(tab_widget, "update_odds"):
                    tab_widget.update_odds(odds, league_name)
    



class DetachableTabBar(QTabBar):
    detachRequested = pyqtSignal(int)  # Signal emitted when a tab is detached

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMovable(True)
        self.drag_start_pos = None  # Initialize drag start position

    def mousePressEvent(self, event):
        """Handle mouse press events for dragging"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_start_pos = event.pos()  # Store the starting position of the drag
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """Handle mouse move events for dragging"""
        if event.buttons() == Qt.MouseButton.LeftButton:
            if self.drag_start_pos is not None:
                # Check if the mouse has moved far enough to start a drag
                if (event.pos() - self.drag_start_pos).manhattanLength() > 10:  # Drag threshold
                    index = self.tabAt(self.drag_start_pos)
                    if index != -1:  # Ensure a valid tab is being dragged
                        self.detachRequested.emit(index)
                        self.drag_start_pos = None  # Reset drag start position
        super().mouseMoveEvent(event)


class DetachedTabWidget(QWidget):
    def __init__(self, league_name, sport_key, data_manager, detached_table_widget, parent=None):
        super().__init__(parent)
        self.league_name = league_name
        self.sport_key = sport_key
        self.data_manager = data_manager

        layout = QVBoxLayout(self)
        # Instead of creating a new QTableWidget, use the detached one:
        self.table_widget = detached_table_widget
        # Reparent the detached widget into this widget's layout
        self.table_widget.setParent(self)
        layout.addWidget(self.table_widget)
        
        # Optionally, connect to data updates if needed:
        self.data_manager.odds_updated.connect(self.update_odds)


    def update_odds(self, odds, league_name):
        """Update the table with new odds data"""
        if league_name == self.league_name:
            # Clear the table
            self.table_widget.clear()
            
            # Set up table headers
            self.table_widget.setColumnCount(2)
            self.table_widget.setHorizontalHeaderLabels(["Market/Outcome", "Odds"])
            
            # Process odds data and populate the table
            if odds and 'bookmakers' in odds:
                row = 0
                for bm in odds['bookmakers']:
                    for market in bm['markets']:
                        for outcome in market['outcomes']:
                            label = f"{outcome['name']} ({outcome.get('point', '')})"
                            price = str(outcome.get('price', ''))
                            
                            self.table_widget.insertRow(row)
                            self.table_widget.setItem(row, 0, QTableWidgetItem(label))
                            self.table_widget.setItem(row, 1, QTableWidgetItem(price))
                            row += 1

# Have to redefine this class instead of importing to avoid circular import cooler
class UpdateProgressIndicator(QWidget):
     def __init__(self, parent=None):
         print("EOdetach UpdateProgressIndicator constructed")
         super().__init__(parent)
         self.setFixedSize(20, 20)
         self.progress = 0
 
     def setProgress(self, value):
         """Set the progress value (0 to 1) and trigger a repaint"""
         print("EOdetach UpdateProgressIndicator setProgress")
         self.progress = value
         self.update()  # Trigger a repaint
 
     def paintEvent(self, event):
         print("EOdetach UpdateProgressIndicator paintevent")
         painter = QPainter(self)  # Automatically begins painting
         if not painter.isActive():
             print("UpdateProgressIndicator: Painter failed to start")
             return
     
         painter.setRenderHint(QPainter.RenderHint.Antialiasing)
         
         # Draw background circle
         painter.setPen(QPen(QColor("#e9ecef"), 2))
         painter.drawEllipse(2, 2, 16, 16)
         
         # Draw progress arc
         painter.setPen(QPen(QColor("#007bff"), 2))
         angle = int(-self.progress * 360)
         painter.drawArc(2, 2, 16, 16, 90 * 16, angle * 16)
         
         painter.end()

        
        # IF YOU REORDER THESE TWO FUNCTIONS CALLS IN SHOWEVENT, THE PROGRAM DOESNT CRASH BUT TAB STILL GONE
     def showEvent(self, event):
         """Trigger a repaint when the widget becomes visible"""
         super().showEvent(event) 
         self.update()  # Repaint the widget
           
