import sys
from PyQt6.QtWidgets import (
    QApplication, QWidget, QGridLayout, QLabel,
    QDoubleSpinBox, QLineEdit, QGroupBox, QVBoxLayout, QHBoxLayout
)
from fractions import Fraction
from PyQt6.QtGui import QIcon, QFont
import pathlib

def american_to_decimal(odds):
    if odds > 0:
        return (odds + 100) / 100
    else:
        return (100 + abs(odds)) / abs(odds)

def decimal_to_american(decimal):
    if decimal >= 2:
        return (decimal - 1) * 100
    else:
        return -100 / (decimal - 1)

def decimal_to_fractional(decimal):
    frac = decimal - 1
    f = Fraction(frac).limit_denominator(100)
    return f

def implied_prob_american(odds):
    if odds > 0:
        return 100 / (odds + 100)
    else:
        return -odds / (-odds + 100)

def devigger(prob1, prob2):
    total_prob = prob1 + prob2
    if total_prob <= 1:
        return prob1, prob2  # No vig to remove or negative vig
    return prob1 / total_prob, prob2 / total_prob

def prob_to_american_odds(prob):
    if prob > 0.5:
        return -100 * prob / (1 - prob)
    else:
        return 100 * (1 - prob) / prob

class OddsLine(QWidget):
    def __init__(self, label: str):
        super().__init__()
        self.label = label
        self.layout = QGridLayout(self)

        # Title row
        title_label = QLabel(label)
        title_font = QFont()
        title_font.setBold(True)
        title_label.setFont(title_font)
        self.layout.addWidget(title_label, 0, 0, 1, 2)
        
        # Small info label above American field
        self.info_label = QLabel("")
        self.info_label.setStyleSheet("""
            QLabel {
                font-size: 12px;
                font-weight: bold;
                color: #2e7d32;
                padding: 2px 4px;
                background-color: rgba(46, 125, 50, 0.1);
                border-radius: 4px;
                border: 1px solid rgba(46, 125, 50, 0.3);
            }
        """)
        self.layout.addWidget(self.info_label, 1, 1)

        # Input fields
        self.american = QLineEdit()
        self.decimal = QLineEdit()
        self.fractional = QLineEdit()
        self.implied = QLineEdit()
        self.to_win = QLineEdit()
        self.payout = QLineEdit()

        self.implied.setReadOnly(True)
        self.to_win.setReadOnly(True)
        self.payout.setReadOnly(True)

        self.layout.addWidget(QLabel("American:"), 2, 0)
        self.layout.addWidget(self.american, 2, 1)
        self.layout.addWidget(QLabel("Decimal:"), 3, 0)
        self.layout.addWidget(self.decimal, 3, 1)
        self.layout.addWidget(QLabel("Fractional:"), 4, 0)
        self.layout.addWidget(self.fractional, 4, 1)
        self.layout.addWidget(QLabel("Implied %:"), 5, 0)
        self.layout.addWidget(self.implied, 5, 1)
        self.layout.addWidget(QLabel("To Win:"), 6, 0)
        self.layout.addWidget(self.to_win, 6, 1)
        self.layout.addWidget(QLabel("Payout:"), 7, 0)
        self.layout.addWidget(self.payout, 7, 1)

class OddsConverterWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Odds Converter with Devigging")

        icon_path = pathlib.Path(__file__).parent / "AppIcon.png"
        self.setWindowIcon(QIcon(str(icon_path)))

        self.resize(500, 400)

        main_layout = QVBoxLayout(self)
        
        # Bet amount - centered and larger
        bet_layout = QHBoxLayout()
        bet_layout.addStretch()
        
        bet_label = QLabel("Bet Amount:")
        bet_label.setStyleSheet("""
            QLabel {
                font-size: 14px;
                font-weight: bold;
                color: #fff;
                margin-right: 10px;
            }
        """)
        
        self.bet_amount = QDoubleSpinBox()
        self.bet_amount.setPrefix("$")
        self.bet_amount.setValue(100)
        self.bet_amount.setMaximum(1e6)
        self.bet_amount.setStyleSheet("""
            QDoubleSpinBox {
                font-size: 14px;
                font-weight: bold;
                padding: 8px;
                border: 2px solid #555;
                border-radius: 6px;
                background-color: rgba(255, 255, 255, 0.1);
                color: white;
                min-width: 120px;
            }
            QDoubleSpinBox:focus {
                border-color: #4CAF50;
            }
        """)
        
        bet_layout.addWidget(bet_label)
        bet_layout.addWidget(self.bet_amount)
        bet_layout.addStretch()
        main_layout.addLayout(bet_layout)

        # Lines side by side
        lines_layout = QHBoxLayout()
        
        self.line1 = OddsLine("Line 1")
        self.line2 = OddsLine("Line 2")

        group1 = QGroupBox()
        group1.setStyleSheet("""
            QGroupBox {
                border: 2px solid #444;
                border-radius: 8px;
                margin: 5px;
                padding-top: 10px;
                background-color: rgba(255, 255, 255, 0.05);
            }
        """)
        group1.setLayout(self.line1.layout)
        
        group2 = QGroupBox()
        group2.setStyleSheet("""
            QGroupBox {
                border: 2px solid #444;
                border-radius: 8px;
                margin: 5px;
                padding-top: 10px;
                background-color: rgba(255, 255, 255, 0.05);
            }
        """)
        group2.setLayout(self.line2.layout)

        lines_layout.addWidget(group1)
        lines_layout.addWidget(group2)
        main_layout.addLayout(lines_layout)

        # Vig display at bottom
        vig_container = QGroupBox()
        vig_container.setStyleSheet("""
            QGroupBox {
                border: 2px solid #555;
                border-radius: 8px;
                margin-top: 10px;
                background-color: rgba(0, 0, 0, 0.1);
            }
        """)
        vig_layout = QHBoxLayout()
        vig_layout.setContentsMargins(10, 10, 10, 10)
        
        vig_title = QLabel("Vigorish:")
        vig_title.setStyleSheet("""
            QLabel {
                font-size: 14px;
                font-weight: bold;
                color: #fff;
                padding: 5px;
                min-width: 80px;
            }
        """)
        
        # Container for the progress bar effect
        self.vig_bar_container = QWidget()
        self.vig_bar_container.setFixedHeight(40)
        self.vig_bar_container.setStyleSheet("""
            QWidget {
                background-color: rgba(255, 255, 255, 0.1);
                border-radius: 6px;
                border: 1px solid #666;
            }
        """)
        
        # The actual vig display label that will act as the "bar"
        self.vig_display = QLabel("")
        vig_font = QFont()
        vig_font.setPointSize(16)
        vig_font.setBold(True)
        self.vig_display.setFont(vig_font)
        self.vig_display.setStyleSheet("""
            QLabel {
                color: white;
                font-size: 16pt;
                font-weight: bold;
                padding: 8px 12px;
                border-radius: 6px;
                background-color: rgba(255, 255, 255, 0.1);
                border: none;
            }
        """)
        
        # Layout for the bar container
        bar_layout = QHBoxLayout(self.vig_bar_container)
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.addWidget(self.vig_display)
        bar_layout.addStretch()
        
        vig_layout.addWidget(vig_title)
        vig_layout.addWidget(self.vig_bar_container, 1)  # Give it stretch factor
        vig_container.setLayout(vig_layout)
        main_layout.addWidget(vig_container)

        # Connect signals
        for line in [self.line1, self.line2]:
            line.american.editingFinished.connect(self.handle)
            line.decimal.editingFinished.connect(self.handle)
            line.fractional.editingFinished.connect(self.handle)

        self.bet_amount.valueChanged.connect(self.handle)

    def get_vig_color(self, vig_percent):
        if vig_percent < 2:
            return "#4CAF50", "white"  # Green background, white text
        elif vig_percent < 5:
            return "#FFC107", "black"  # Yellow background, black text
        elif vig_percent < 8:
            return "#FF9800", "white"  # Orange background, white text
        else:
            return "#F44336", "white"  # Red background, white text

    def handle(self):
        for line in [self.line1, self.line2]:
            try:
                ao = float(line.american.text())
            except ValueError:
                try:
                    dec = float(line.decimal.text())
                    ao = decimal_to_american(dec)
                    line.american.setText(f"{int(ao)}")
                except ValueError:
                    try:
                        num, den = map(int, line.fractional.text().split('/'))
                        dec = num / den + 1
                        ao = decimal_to_american(dec)
                        line.american.setText(f"{int(ao)}")
                    except:
                        continue

            dec = american_to_decimal(ao)
            line.decimal.setText(f"{dec:.2f}")
            frac = decimal_to_fractional(dec)
            line.fractional.setText(f"{frac.numerator}/{frac.denominator}")
            prob = implied_prob_american(ao)
            line.implied.setText(f"{prob*100:.2f}%")

            amt = self.bet_amount.value()
            if ao > 0:
                win = amt * ao / 100
            else:
                win = amt * 100 / abs(ao)
            line.to_win.setText(f"${win:.2f}")
            line.payout.setText(f"${amt + win:.2f}")

        self.update_vig_and_fair_odds()

    def update_vig_and_fair_odds(self):
        try:
            prob1 = float(self.line1.implied.text().strip('%')) / 100
            prob2 = float(self.line2.implied.text().strip('%')) / 100
            vig = (prob1 + prob2 - 1) * 100
            
            # Calculate fair odds
            devigged_prob1, devigged_prob2 = devigger(prob1, prob2)
            fair_odds1 = prob_to_american_odds(devigged_prob1)
            fair_odds2 = prob_to_american_odds(devigged_prob2)
            
            # Calculate progress bar width (0% = 0 width, 15% = 100% width)
            max_vig = 15.0
            vig_percentage = max(0, min(abs(vig), max_vig)) / max_vig  # Normalize to 0-1
            
            # Get color based on vig level - smooth gradient
            if vig_percentage <= 0.133:  # 0-2%
                color = "#4CAF50"  # Green
            elif vig_percentage <= 0.333:  # 2-5%
                color = "#8BC34A"  # Light green
            elif vig_percentage <= 0.533:  # 5-8%
                color = "#FFC107"  # Yellow
            elif vig_percentage <= 0.733:  # 8-11%
                color = "#FF9800"  # Orange
            else:  # 11%+
                color = "#F44336"  # Red
            
            # Calculate the width as percentage of container
            bar_width_percent = int(vig_percentage * 100)
            
            # Update the vig display with progress bar effect
            self.vig_display.setText(f"{vig:.1f}%")
            
            # Create gradient effect by adjusting the width
            self.vig_bar_container.setStyleSheet(f"""
                QWidget {{
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                        stop:0 {color}, 
                        stop:{vig_percentage:.3f} {color},
                        stop:{vig_percentage:.3f} rgba(255, 255, 255, 0.1),
                        stop:1 rgba(255, 255, 255, 0.1));
                    border-radius: 6px;
                    border: 1px solid #666;
                }}
            """)
            
            self.vig_display.setStyleSheet(f"""
                QLabel {{
                    color: white;
                    font-size: 16pt;
                    font-weight: bold;
                    padding: 8px 12px;
                    border-radius: 6px;
                    background-color: transparent;
                    border: none;
                }}
            """)
            
            # Update Line 1 - show its fair odds with enhanced styling
            fair1_text = f"{int(fair_odds1):+d}" if fair_odds1 > 0 else f"{int(fair_odds1)}"
            self.line1.info_label.setText(f"Fair: {fair1_text}")
            
            # Update Line 2 - show its fair odds with enhanced styling
            fair2_text = f"{int(fair_odds2):+d}" if fair_odds2 > 0 else f"{int(fair_odds2)}"
            self.line2.info_label.setText(f"Fair: {fair2_text}")
            
        except:
            # Clear labels if calculation fails
            self.vig_display.clear()
            self.vig_bar_container.setStyleSheet("""
                QWidget {
                    background-color: rgba(255, 255, 255, 0.1);
                    border-radius: 6px;
                    border: 1px solid #666;
                }
            """)
            self.line1.info_label.clear()
            self.line2.info_label.clear()

if __name__ == '__main__':
    app = QApplication(sys.argv)
    w = OddsConverterWidget()
    w.show()
    sys.exit(app.exec())
