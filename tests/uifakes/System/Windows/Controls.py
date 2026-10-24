class _Ev(list):
    def __iadd__(self, f): self.append(f); return self
class _L(list):
    def Add(self, x): self.append(x)
class _W:
    def __init__(self): self.Children = _L(); self.Click = _Ev(); self.Text = ""
    def ScrollToEnd(self): pass
class StackPanel(_W): pass
class TextBlock(_W): pass
class TextBox(_W): pass
class Button(_W): pass
class Orientation: Horizontal = "H"
class ScrollBarVisibility: Auto = "Auto"
