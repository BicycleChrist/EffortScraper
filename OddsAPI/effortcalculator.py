import sys
from PyQt6.QtWidgets import (
    QApplication, QWidget, QGridLayout, QLabel,
    QDoubleSpinBox, QLineEdit, QGroupBox, QVBoxLayout
)
from PyQt6.QtCore import Qt
from fractions import Fraction
from PyQt6.QtGui import QIcon
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


def implied_prob_decimal(decimal):
    return 1 / decimal


def implied_prob_fractional(frac):
    return 1 / (frac + 1)


class OddsLine(QWidget):
    def __init__(self, label: str):
        super().__init__()
        self.label = label
        self.layout = QGridLayout(self)


        self.layout.addWidget(QLabel(label), 0, 0, 1, 2)

        self.american = QLineEdit()
        self.decimal = QLineEdit()
        self.fractional = QLineEdit()
        self.implied = QLineEdit()
        self.to_win = QLineEdit()
        self.payout = QLineEdit()

        self.implied.setReadOnly(True)
        self.to_win.setReadOnly(True)
        self.payout.setReadOnly(True)

        self.layout.addWidget(QLabel("American:"), 1, 0)
        self.layout.addWidget(self.american, 1, 1)
        self.layout.addWidget(QLabel("Decimal:"), 2, 0)
        self.layout.addWidget(self.decimal, 2, 1)
        self.layout.addWidget(QLabel("Fractional:"), 3, 0)
        self.layout.addWidget(self.fractional, 3, 1)
        self.layout.addWidget(QLabel("Implied %:"), 4, 0)
        self.layout.addWidget(self.implied, 4, 1)
        self.layout.addWidget(QLabel("To Win:"), 5, 0)
        self.layout.addWidget(self.to_win, 5, 1)
        self.layout.addWidget(QLabel("Payout:"), 6, 0)
        self.layout.addWidget(self.payout, 6, 1)


class OddsConverterWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Odds Converter & Implied Probability")

        icon_path = pathlib.Path(__file__).parent / "AppIcon.png"
        self.setWindowIcon(QIcon(str(icon_path)))

        self.resize(500,500)

        main_layout = QVBoxLayout(self)
        grid = QGridLayout()

        grid.addWidget(QLabel("Bet Amount:"), 0, 0)
        self.bet_amount = QDoubleSpinBox()
        self.bet_amount.setPrefix("$")
        self.bet_amount.setValue(100)
        self.bet_amount.setMaximum(1e6)
        grid.addWidget(self.bet_amount, 0, 1)

        self.line1 = OddsLine("Line 1")
        self.line2 = OddsLine("Line 2")

        group1 = QGroupBox("Line 1")
        group1.setLayout(self.line1.layout)
        group2 = QGroupBox("Line 2")
        group2.setLayout(self.line2.layout)

        self.vig_label = QLabel("Vigorish %: ")
        self.vig_output = QLineEdit()
        self.vig_output.setReadOnly(True)

        vig_layout = QGridLayout()
        vig_layout.addWidget(self.vig_label, 0, 0)
        vig_layout.addWidget(self.vig_output, 0, 1)

        main_layout.addLayout(grid)
        main_layout.addWidget(group1)
        main_layout.addWidget(group2)
        main_layout.addLayout(vig_layout)

        for line in [self.line1, self.line2]:
            line.american.editingFinished.connect(self.handle)
            line.decimal.editingFinished.connect(self.handle)
            line.fractional.editingFinished.connect(self.handle)

        self.bet_amount.valueChanged.connect(self.handle)

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

        self.update_vigorish()

    def update_vigorish(self):
        try:
            prob1 = float(self.line1.implied.text().strip('%')) / 100
            prob2 = float(self.line2.implied.text().strip('%')) / 100
            vig = (prob1 + prob2 - 1) * 100
            self.vig_output.setText(f"{vig:.2f}%")
        except:
            self.vig_output.clear()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    w = OddsConverterWidget()
    w.show()
    sys.exit(app.exec())
