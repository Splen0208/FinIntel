"""Repair OpenCLI 1.8.8 XHS visibility checks without weakening filter uniqueness.

Usage: python repair_xhs_search_visibility.py [path/to/clis/xiaohongshu/search.js]
Already patched files are left unchanged. Unknown layouts abort before any write.
"""
from datetime import datetime
from pathlib import Path
import shutil
import sys


def patched_text(text: str) -> str:
    note = "const style = getComputedStyle(el);"
    option = "const style = getComputedStyle(element);"
    guards = {
        note: "if (el.closest('[aria-hidden=\"true\"]') || Number.parseFloat(style.opacity || '1') < 0.01) return false;",
        option: "if (element.closest('[aria-hidden=\"true\"]') || Number.parseFloat(style.opacity || '1') < 0.01) return false;",
    }
    if text.count(note) != 2 or text.count(option) != 1:
        raise ValueError('Unknown adapter layout; inspect the installed version before patching')
    present = [text.count(guards[note]), text.count(guards[option])]
    if present == [2, 1]:
        return text
    if present != [0, 0] or 'Number.parseFloat(style.opacity' in text:
        raise ValueError('Partial or unknown visibility patch; no file was changed')
    for marker, guard in guards.items():
        text = text.replace(marker, marker+'\n          '+guard)
    return text


def main():
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path.home()/'.opencli/node_modules/@jackwener/opencli/clis/xiaohongshu/search.js')
    original = target.read_text(encoding='utf-8')
    fixed = patched_text(original)
    if fixed == original:
        print('Visibility patch already installed; no changes')
        return
    backup = target.with_name(target.name+'.before-visibility-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    shutil.copy2(target, backup)
    target.write_text(fixed, encoding='utf-8', newline='\n')
    print('Patched three visibility checks; backup: '+str(backup))


if __name__ == '__main__':
    main()
