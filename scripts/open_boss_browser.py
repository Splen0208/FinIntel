"""Open the project's dedicated browser for the user's own login."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import store
import managed_browser
store.apply_migrations()
endpoint=managed_browser.ensure()
print('Dedicated BOSS Chrome: '+endpoint)
print('Complete login and platform verification yourself in this window, then return to FinIntel.')
