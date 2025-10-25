#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import ebooklib
from ebooklib import epub
from main import get_spine_doc_items, calculate_total_chars

def debug_partial_creation():
    print("=== 調試 partial 建立過程 ===")
    
    book = epub.read_epub('城邦：14100128638.epub')
    spine_items = get_spine_doc_items(book)
    total_chars, chapter_lengths, chapter_texts = calculate_total_chars(spine_items)
    
    target = int(total_chars * 0.1)
    collected = 0
    
    print(f"目標字元數: {target}")
    
    for idx, item in enumerate(spine_items):
        ch_len = chapter_lengths[idx]
        ch_txt = chapter_texts[idx]
        
        print(f"\nidx={idx}, ID={item.get_id()}")
        print(f"  字元數: {ch_len}")
        print(f"  累積: {collected}")
        print(f"  文字預覽: {repr(ch_txt[:50])}")
        
        if collected + ch_len < target:
            collected += ch_len
            print(f"  → 包含完整章節，新累積={collected}")
        else:
            remain = target - collected
            partial_text = ch_txt[:remain] if remain > 0 else ""
            print(f"  → 建立 partial_{idx+1}")
            print(f"  → remain = {target} - {collected} = {remain}")
            print(f"  → partial_text 長度: {len(partial_text)}")
            print(f"  → partial_text 預覽: {repr(partial_text[:100])}")
            break

if __name__ == "__main__":
    debug_partial_creation()
