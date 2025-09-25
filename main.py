# -*- coding: utf-8 -*-
"""
固定擷取 EPUB 前 10%（以「可見文字字元數」計），輸出新的 EPUB。
- 封面頁固定在最前面（若辨識到封面圖片）
- 新書標題沿用原書（不加「節錄 10%」字樣）
- 圖片 / 字型 / CSS 等資源「全數帶入」（允許重複打包）
- 不修改章節標題（不再加「章節 1」「（節錄）」等字樣）
- 最後一章以純文字精準截斷（不加任何「節錄」標題）
- Nav 目錄頁照常加入，但會放在 spine 的最後，避免成為第一頁

使用方式：
python main.py input.epub
# 指定輸出檔名
python main.py input.epub --output my_sample.epub
# 若不想加入封面頁
python main.py input.epub --no-cover
"""

import os
import sys
import math
import argparse
import html
import zipfile
import tempfile
import shutil
from typing import List, Tuple, Optional

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup

# ---------- 文字擷取與章節處理 ----------

def extract_visible_text(xhtml_bytes: bytes) -> str:
    """從 XHTML 內容取出可閱讀文字（去除 script/style/noscript），保留基本段落換行。"""
    soup = BeautifulSoup(xhtml_bytes, "xml")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = soup.get_text(separator="\n", strip=True)
    lines = [ln.strip() for ln in text.splitlines()]
    cleaned = "\n".join([ln for ln in lines if ln])
    return cleaned

def get_spine_doc_items(book: epub.EpubBook) -> List[epub.EpubItem]:
    """依 spine 順序取得文件（XHTML/HTML）項目。"""
    id_to_item = {it.get_id(): it for it in book.get_items_of_type(ebooklib.ITEM_DOCUMENT)}
    spine_ids = [s[0] for s in book.spine]  # [('chapter_1', {}), ...] -> 'chapter_1'
    items = []
    for sid in spine_ids:
        item = id_to_item.get(sid)
        if item is not None:
            items.append(item)
    # 若 spine 為空，退回所有文件（少數不規範 EPUB）
    if not items:
        items = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))
    return items

def calculate_total_chars(items: List[epub.EpubItem]) -> Tuple[int, List[int], List[str]]:
    """回傳總字元數、每章字元數、每章純文字。"""
    chapter_texts, chapter_lengths = [], []
    total = 0
    for it in items:
        txt = extract_visible_text(it.get_content())
        chapter_texts.append(txt)
        L = len(txt)
        chapter_lengths.append(L)
        total += L
    return total, chapter_lengths, chapter_texts

def build_partial_xhtml_from_original(original_content: bytes, target_chars: int, title: str = "", css_files: List[str] = None) -> bytes:
    """從原始 XHTML 內容中截取指定字元數，保留原始排版和結構。"""
    try:
        soup = BeautifulSoup(original_content, "xml")
        
        # 保留原始的 head 部分（包含 CSS 引用等）
        head = soup.find('head')
        if head:
            if title:
                title_tag = head.find('title')
                if title_tag:
                    title_tag.string = title
                else:
                    new_title = soup.new_tag('title')
                    new_title.string = title
                    head.append(new_title)
            
            # 添加 CSS 引用
            if css_files:
                existing_links = head.find_all('link', {'rel': 'stylesheet'})
                existing_hrefs = {link.get('href') for link in existing_links}
                
                for css_file in css_files:
                    if css_file not in existing_hrefs:
                        link = soup.new_tag('link')
                        link['rel'] = 'stylesheet'
                        link['type'] = 'text/css'
                        link['href'] = css_file
                        head.append(link)
        
        # 處理 body 內容
        body = soup.find('body')
        if not body:
            # 如果沒有 body，創建一個簡單的結構
            return build_partial_xhtml_fallback(title, extract_visible_text(original_content)[:target_chars])
        
        # 計算當前可見文字長度並截斷
        current_chars = 0
        elements_to_remove = []
        
        # 遞歸處理所有文本節點
        def process_element(element):
            nonlocal current_chars, target_chars
            if current_chars >= target_chars:
                return True  # 已達到目標，標記後續元素刪除
            
            # 如果是純文本節點 (NavigableString)
            if hasattr(element, 'string') and element.string and not hasattr(element, 'name'):
                text = element.string.strip()
                if text:
                    if current_chars + len(text) <= target_chars:
                        current_chars += len(text)
                        return False
                    else:
                        # 需要截斷這個文本節點
                        remaining = target_chars - current_chars
                        if remaining > 0:
                            element.string.replace_with(text[:remaining])
                            current_chars = target_chars
                        else:
                            element.extract()
                        return True
            
            # 如果是元素節點，遞歸處理子元素
            if hasattr(element, 'children'):
                children_to_remove = []
                for child in list(element.children):  # 使用 list() 避免迭代時修改
                    should_remove = process_element(child)
                    if should_remove:
                        children_to_remove.append(child)
                
                # 移除標記的子元素
                for child in children_to_remove:
                    child.extract()
            
            return current_chars >= target_chars
        
        process_element(body)
        
        # 確保 HTML 結構完整
        if not soup.find('html'):
            html_tag = soup.new_tag('html')
            html_tag['xmlns'] = "http://www.w3.org/1999/xhtml"
            html_tag['lang'] = "zh"
            for child in list(soup.children):
                html_tag.append(child)
            soup.append(html_tag)
        
        # 添加 XML 聲明
        result = '<?xml version="1.0" encoding="utf-8"?>\n' + str(soup)
        return result.encode("utf-8")
        
    except Exception as e:
        print(f"處理原始 XHTML 時發生錯誤: {e}")
        # 回退到純文字處理
        return build_partial_xhtml_fallback(title, extract_visible_text(original_content)[:target_chars])

def build_partial_xhtml_fallback(title: str, text: str) -> bytes:
    """純文字回退方案：把純文字包成最簡 XHTML（僅以 <p> 分段，不加任何標題/裝飾）。"""
    paragraphs = [html.escape(p) for p in text.splitlines() if p.strip()]
    body = "".join(f"<p>{p}</p>\n" for p in paragraphs) if paragraphs else "<p></p>"
    xhtml = f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="zh">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title or "")}</title>
</head>
<body>
{body}
</body>
</html>
'''
    return xhtml.encode("utf-8")

# ---------- 封面偵測與建立封面頁 ----------

def find_cover_image_item(book: epub.EpubBook) -> Optional[epub.EpubItem]:
    """嘗試找出原書的『封面圖片』資源。回傳對應的 image item（若找到）。"""
    # 1) EPUB2：<meta name="cover" content="cover-image-id" />
    try:
        meta_cover = book.get_metadata("OPF", "cover")
        if meta_cover:
            cover_id = meta_cover[0][1]['content']  # 修正：取content屬性值
            print(f"EPUB2 cover ID: {cover_id}")
            for it in book.get_items():
                if it.get_id() == cover_id and it.media_type and it.media_type.startswith("image/"):
                    print(f"找到封面圖片 (EPUB2 meta): {it.file_name}")
                    return it
    except Exception as e:
        print(f"EPUB2 cover check error: {e}")
        pass

    # 2) EPUB3：manifest item properties 含 'cover-image'
    try:
        for it in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            props = getattr(it, "properties", None)
            if props and ("cover-image" in props or "cover" in props):
                print(f"找到封面圖片 (EPUB3 properties): {it.file_name}")
                return it
    except Exception:
        pass

    # 3) 檔名啟發式（cover/封面）
    try:
        candidates = []
        for it in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            name = (it.file_name or "").lower()
            if "cover" in name or "封面" in name:
                candidates.append(it)
        if candidates:
            candidates.sort(key=lambda x: len(x.get_content() or b""), reverse=True)
            print(f"找到封面圖片 (檔名啟發): {candidates[0].file_name}")
            return candidates[0]
    except Exception:
        pass

    # 4) Fallback：使用第一張或最大的圖片作為封面
    try:
        all_images = list(book.get_items_of_type(ebooklib.ITEM_IMAGE))
        if all_images:
            # 優先選擇最大的圖片（通常封面圖片較大）
            largest_image = max(all_images, key=lambda x: len(x.get_content() or b""))
            print(f"找到封面圖片 (Fallback - 最大圖片): {largest_image.file_name}")
            return largest_image
    except Exception:
        pass

    print("未找到封面圖片")
    return None

def create_cover_html(cover_href: str) -> epub.EpubHtml:
    """建立一個簡單的封面頁，引用已存在於書內的封面圖片資源。"""
    cover_html = epub.EpubHtml(uid="cover_page", file_name="cover.xhtml", title="封面", lang="zh")
    
    # 使用內聯樣式，避免ebooklib移除樣式
    html_content = f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="zh">
<head>
  <meta charset="utf-8"/>
  <title>封面</title>
</head>
<body style="margin: 0; padding: 0; text-align: center; background-color: #f5f5f5;">
  <div style="display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 20px; box-sizing: border-box;">
    <img src="{html.escape(cover_href)}" alt="Cover" style="max-width: 100%; max-height: 90vh; height: auto; box-shadow: 0 4px 8px rgba(0,0,0,0.1);"/>
  </div>
</body>
</html>'''
    
    cover_html.content = html_content.encode("utf-8")
    return cover_html

# ---------- 新 EPUB 的組裝 ----------

def clone_asset_item(src_item: epub.EpubItem) -> epub.EpubItem:
    """複製非文件資源（圖片/CSS/字型/...）到新書（允許重複打包）。"""
    cloned = epub.EpubItem(
        uid=src_item.get_id(),
        file_name=src_item.file_name,
        media_type=src_item.media_type,
        content=src_item.get_content()
    )
    
    # 保留其他可能的屬性
    try:
        if hasattr(src_item, 'properties') and src_item.properties:
            cloned.properties = src_item.properties
        if hasattr(src_item, 'media_overlay') and src_item.media_overlay:
            cloned.media_overlay = src_item.media_overlay
        if hasattr(src_item, 'fallback') and src_item.fallback:
            cloned.fallback = src_item.fallback
    except Exception as e:
        print(f"複製資源屬性時發生警告 {src_item.file_name}: {e}")
    
    return cloned

def add_css_to_xhtml(content: bytes, css_files: List[str]) -> bytes:
    """在 XHTML 內容的 head 部分添加 CSS 引用。"""
    if not css_files:
        print("沒有 CSS 文件需要添加")
        return content
        
    print(f"開始為文件添加 CSS 引用: {css_files}")
    try:
        soup = BeautifulSoup(content, "xml")
        head = soup.find('head')
        print(f"找到 head 標籤: {head}")
        
        if head:
            # 檢查是否已經有 CSS 引用
            existing_links = head.find_all('link', {'rel': 'stylesheet'})
            existing_hrefs = {link.get('href') for link in existing_links}
            print(f"現有 CSS 引用: {existing_hrefs}")
            
            # 添加缺少的 CSS 引用
            for css_file in css_files:
                if css_file not in existing_hrefs:
                    link = soup.new_tag('link')
                    link['rel'] = 'stylesheet'
                    link['type'] = 'text/css'
                    link['href'] = css_file
                    head.append(link)
                    print(f"已添加 CSS 引用到 head: {css_file}")
                else:
                    print(f"CSS 引用已存在，跳過: {css_file}")
        else:
            # 如果沒有 head 標籤，創建一個
            html_tag = soup.find('html')
            if html_tag:
                new_head = soup.new_tag('head')
                for css_file in css_files:
                    link = soup.new_tag('link')
                    link['rel'] = 'stylesheet'
                    link['type'] = 'text/css'
                    link['href'] = css_file
                    new_head.append(link)
                    print(f"已創建 head 並添加 CSS 引用: {css_file}")
                
                # 將 head 插入到 html 標籤的開始
                html_tag.insert(0, new_head)
        
        # 確保有 XML 聲明
        result = str(soup)
        if not result.startswith('<?xml'):
            result = '<?xml version="1.0" encoding="utf-8"?>\n' + result
        
        print(f"CSS 添加完成，head 部分: {soup.find('head')}")
        return result.encode("utf-8")
    except Exception as e:
        print(f"添加 CSS 引用時發生錯誤: {e}")
        import traceback
        traceback.print_exc()
        return content

def clone_document_item(src_item: epub.EpubHtml, title: str = None, css_files: List[str] = None) -> epub.EpubHtml:
    """複製完整文件章節（保持原檔名/媒體型別/標題/所有屬性），並自動添加 CSS 引用。"""
    cloned = epub.EpubHtml(
        uid=src_item.get_id(),
        file_name=src_item.file_name,
        media_type=src_item.media_type,
        title=title if title is not None else (getattr(src_item, "title", "") or "")
    )
    
    # 完整複製內容並添加 CSS 引用
    original_content = src_item.get_content()
    if css_files:
        modified_content = add_css_to_xhtml(original_content, css_files)
        cloned.content = modified_content
        print(f"已為 {src_item.file_name} 添加 CSS 引用，內容長度: {len(modified_content)}")
    else:
        cloned.content = original_content
    
    # 保留所有可能的屬性
    try:
        if hasattr(src_item, 'lang') and src_item.lang:
            cloned.lang = src_item.lang
        if hasattr(src_item, 'direction') and src_item.direction:
            cloned.direction = src_item.direction
        if hasattr(src_item, 'properties') and src_item.properties:
            cloned.properties = src_item.properties
        if hasattr(src_item, 'media_overlay') and src_item.media_overlay:
            cloned.media_overlay = src_item.media_overlay
        if hasattr(src_item, 'fallback') and src_item.fallback:
            cloned.fallback = src_item.fallback
    except Exception as e:
        print(f"複製文件屬性時發生警告 {src_item.file_name}: {e}")
    
    return cloned

def post_process_epub_css(epub_path: str, css_files: List[str]) -> None:
    """後處理 EPUB 文件，在所有 XHTML 文件中添加 CSS 引用，並修正排版問題。"""
    if not css_files:
        return
    
    print(f"開始後處理 EPUB 文件，添加 CSS 引用和修正排版: {css_files}")
    
    # 創建臨時目錄
    with tempfile.TemporaryDirectory() as temp_dir:
        # 解壓 EPUB
        with zipfile.ZipFile(epub_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
        
        # 處理所有 XHTML 文件
        for root, dirs, files in os.walk(temp_dir):
            for file in files:
                if file.endswith('.xhtml') or file.endswith('.html'):
                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                        
                        # 解析並添加 CSS 引用
                        soup = BeautifulSoup(content, 'xml')
                        head = soup.find('head')
                        html_tag = soup.find('html')
                        
                        # 檢查是否需要添加 page class（針對書名頁等橫排內容）
                        needs_page_class = False
                        if html_tag and soup.find(class_='tittlepage'):
                            needs_page_class = True
                            print(f"檢測到 {file} 包含 tittlepage，需要橫排顯示")
                        
                        # 為 html 標籤添加 page class
                        if needs_page_class and html_tag:
                            current_class = html_tag.get('class', [])
                            if isinstance(current_class, str):
                                current_class = [current_class]
                            elif current_class is None:
                                current_class = []
                            
                            if 'page' not in current_class:
                                current_class.append('page')
                                html_tag['class'] = current_class
                                print(f"已為 {file} 的 html 標籤添加 page class")
                        
                        if head:
                            # 檢查現有的 CSS 引用
                            existing_links = head.find_all('link', {'rel': 'stylesheet'})
                            existing_hrefs = {link.get('href') for link in existing_links}
                            
                            # 添加缺少的 CSS 引用
                            added_css = False
                            for css_file in css_files:
                                if css_file not in existing_hrefs:
                                    link = soup.new_tag('link')
                                    link['rel'] = 'stylesheet'
                                    link['type'] = 'text/css'
                                    link['href'] = css_file
                                    head.append(link)
                                    added_css = True
                            
                            if added_css or needs_page_class:
                                # 寫回文件
                                with open(file_path, 'w', encoding='utf-8') as f:
                                    f.write(str(soup))
                                print(f"已更新 {file}")
                    
                    except Exception as e:
                        print(f"處理文件 {file} 時發生錯誤: {e}")
        
        # 重新打包 EPUB
        with zipfile.ZipFile(epub_path, 'w', zipfile.ZIP_DEFLATED) as zip_ref:
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arc_path = os.path.relpath(file_path, temp_dir)
                    zip_ref.write(file_path, arc_path)
    
    print("EPUB 後處理完成")

def make_epub_sample_10pct(input_path: str, output_path: str, include_cover: bool = True, complete_chapters_only: bool = False) -> str:
    """建立新的 EPUB，內容為前 10%（最後一章以純文字截斷），封面可選擇加入且置於最前。"""
    book = epub.read_epub(input_path)
    spine_items = get_spine_doc_items(book)
    total_chars, chapter_lengths, chapter_texts = calculate_total_chars(spine_items)

    if total_chars == 0:
        raise ValueError("偵測不到可讀文字（可能是全圖片或受 DRM/加密保護）。")

    target = max(1, math.floor(total_chars * 0.10))

    new_book = epub.EpubBook()

    # 基本中繼資料：沿用原書
    titles = book.get_metadata("DC", "title")
    base_title = titles[0][0] if titles else os.path.splitext(os.path.basename(input_path))[0]
    new_book.set_title(base_title)

    langs = book.get_metadata("DC", "language")
    new_book.set_language(langs[0][0] if langs else "zh")

    for a in book.get_metadata("DC", "creator"):
        new_book.add_author(a[0])

    # 先處理封面圖片（如果需要的話）
    cover_image_item = None
    if include_cover:
        cover_image_item = find_cover_image_item(book)
        if cover_image_item:
            # 確保封面圖片被複製到新書中
            try:
                cloned_cover = clone_asset_item(cover_image_item)
                new_book.add_item(cloned_cover)
                print(f"已複製封面圖片: {cloned_cover.file_name}")
            except Exception as e:
                print(f"複製封面圖片失敗: {e}")
                cover_image_item = None

    # 整本資源帶入（圖片/樣式/字型/影音等），避免重複打包
    print("開始複製所有資源文件...")
    asset_types = [
        ebooklib.ITEM_IMAGE,
        ebooklib.ITEM_STYLE,
        ebooklib.ITEM_FONT,
        ebooklib.ITEM_VIDEO,
        ebooklib.ITEM_AUDIO,
        ebooklib.ITEM_VECTOR,
        ebooklib.ITEM_UNKNOWN,
    ]
    
    # 記錄已添加的項目，避免重複
    added_items = set()
    for existing_item in new_book.get_items():
        added_items.add(existing_item.get_id())
    
    copied_assets = 0
    for t in asset_types:
        for item in book.get_items_of_type(t):
            # 檢查是否已經添加過（例如封面圖片）
            if item.get_id() not in added_items:
                try:
                    cloned_asset = clone_asset_item(item)
                    new_book.add_item(cloned_asset)
                    added_items.add(item.get_id())
                    copied_assets += 1
                    print(f"已複製資源: {item.file_name} ({item.media_type})")
                except Exception as e:
                    print(f"複製資源失敗 {item.file_name}: {e}")
            else:
                print(f"跳過重複資源: {item.file_name} (已存在)")
    
    print(f"總共複製了 {copied_assets} 個資源文件")
    
    # 額外檢查：確保所有在 manifest 中的項目都被包含
    try:
        all_items = list(book.get_items())
        manifest_items = [item for item in all_items if not isinstance(item, epub.EpubHtml)]
        for item in manifest_items:
            if item.get_id() not in added_items:
                try:
                    new_book.add_item(clone_asset_item(item))
                    added_items.add(item.get_id())
                    print(f"補充複製遺漏的資源: {item.file_name}")
                except Exception as e:
                    print(f"補充複製資源失敗 {item.file_name}: {e}")
    except Exception as e:
        print(f"檢查遺漏資源時發生錯誤: {e}")

    # 收集所有 CSS 文件路徑
    css_files = []
    for item in book.get_items_of_type(ebooklib.ITEM_STYLE):
        css_files.append(item.file_name)
    print(f"發現 CSS 文件: {css_files}")

    include_items = []
    new_spine = []

    # 檢查是否已有封面頁存在
    existing_cover_item = None
    existing_cover_index = -1
    if include_cover:
        # 檢查原書是否已有封面頁（通常ID為 cover 或檔名為 cover.xhtml）
        for idx, item in enumerate(spine_items):
            if (item.get_id().lower() in ['cover', 'cover_page'] or 
                'cover' in (item.file_name or '').lower()):
                existing_cover_item = item
                existing_cover_index = idx
                print(f"發現原書已有封面頁: {item.file_name} (ID: {item.get_id()}) at index {idx}")
                break
    
    # 封面頁處理
    if include_cover:
        if existing_cover_item and cover_image_item:
            # 如果原書已有封面頁，優先使用原封面頁，但確保封面圖片存在
            print(f"使用原書封面頁: {existing_cover_item.file_name}")
            # 不建立新封面頁，原封面頁會在後續章節處理中自然包含
        elif cover_image_item and not existing_cover_item:
            # 只有在沒有原封面頁時才建立新封面頁
            try:
                cover_html = create_cover_html(cover_image_item.file_name)
                new_book.add_item(cover_html)
                include_items.append(cover_html)
                new_spine.append(cover_html)   # 封面成為 spine 第一頁
                print(f"已建立封面頁，引用圖片: {cover_image_item.file_name}")
            except Exception as e:
                print(f"建立封面頁失敗: {e}")
        elif existing_cover_item and not cover_image_item:
            # 原書有封面頁但通過標準方法沒找到封面圖片，嘗試使用fallback圖片修復
            try:
                all_images = list(book.get_items_of_type(ebooklib.ITEM_IMAGE))
                if all_images:
                    # 使用最大的圖片作為fallback封面
                    fallback_image = max(all_images, key=lambda x: len(x.get_content() or b""))
                    print(f"原封面頁引用圖片不存在，使用fallback圖片修復封面頁: {fallback_image.file_name}")
                    cover_html = create_cover_html(fallback_image.file_name)
                    new_book.add_item(cover_html)
                    include_items.insert(0, cover_html)  # 插入到最前面
                    new_spine.insert(0, cover_html)     # 封面成為 spine 第一頁
                    print(f"已建立修復的封面頁，引用圖片: {fallback_image.file_name}")
                else:
                    print("使用原書封面頁（儘管引用的圖片可能不存在）")
            except Exception as e:
                print(f"建立修復封面頁失敗: {e}")
                print("使用原書封面頁（儘管引用的圖片可能不存在）")
        else:
            print("未找到封面圖片，跳過封面頁建立")

    # 10% 節錄
    collected = 0
    for idx, it in enumerate(spine_items):
        ch_len = chapter_lengths[idx]
        ch_txt = chapter_texts[idx]

        if collected + ch_len < target:
            # 整章保留原排版與原標題，並添加 CSS 引用
            cloned = clone_document_item(it, title=(getattr(it, "title", "") or f"章節 {idx+1}"), css_files=css_files)
            new_book.add_item(cloned)
            include_items.append(cloned)
            new_spine.append(cloned)
            collected += ch_len
        else:
            if complete_chapters_only:
                # 只包含完整章節，不產生部分章節
                print(f"已包含 {len(include_items)} 個完整章節，總計 {collected} 字元 ({collected/total_chars*100:.1f}%)")
                break
            else:
                # 最後一章：保留原始 HTML 結構並精準截斷；標題沿用原章標題（若無則留空）
                remain = target - collected
                partial_title = getattr(it, "title", "") or ""
                
                # 使用新的函數來保留原始排版
                try:
                    partial_content = build_partial_xhtml_from_original(
                        it.get_content(), 
                        remain, 
                        partial_title,
                        css_files
                    )
                except Exception as e:
                    print(f"使用原始結構處理部分章節失敗，回退到純文字: {e}")
                    partial_text = ch_txt[:remain] if remain > 0 else ""
                    partial_content = build_partial_xhtml_fallback(partial_title, partial_text)
                
                partial_html = epub.EpubHtml(
                    uid=f"partial_{idx+1}",
                    file_name=f"partial_{idx+1}.xhtml",
                    title=partial_title,
                    lang="zh"
                )
                partial_html.content = partial_content
                new_book.add_item(partial_html)
                include_items.append(partial_html)
                new_spine.append(partial_html)
                print(f"已建立保留排版的部分章節: {partial_title} ({remain} 字元)")
                break

    # 導覽與目錄（⚠ 將 nav 放在 spine 的最後，避免成為第一頁）
    # 過濾掉partial頁面，只在TOC中包含完整章節
    # toc_items = [item for item in include_items if not item.get_id().startswith('partial_')]
    # new_book.toc = toc_items
    # new_book.add_item(epub.EpubNcx())
    nav = epub.EpubNav()
    # new_book.add_item(nav)
    new_book.spine = new_spine + ["nav"]   # 讓封面與內容先展示，nav 最後

    epub.write_epub(output_path, new_book)
    
    # 後處理：添加 CSS 引用
    if css_files:
        post_process_epub_css(output_path, css_files)
    
    return output_path

def main():
    ap = argparse.ArgumentParser(description="輸出 EPUB 的前 10% 為新的 EPUB（封面置前，資源全數帶入，章節標題不修改，Nav 不當第一頁）")
    ap.add_argument("input", help="輸入 EPUB 檔案路徑")
    ap.add_argument("--output", help="輸出檔名（預設自動加 _sample.epub）")
    ap.add_argument("--no-cover", action="store_true", help="不加入封面頁")
    ap.add_argument("--complete-chapters-only", action="store_true", help="只包含完整章節，不生成部分章節")
    args = ap.parse_args()

    if not os.path.exists(args.input):
        print(f"找不到檔案：{args.input}", file=sys.stderr)
        sys.exit(1)

    base = os.path.splitext(os.path.basename(args.input))[0]
    out = args.output or f"{base}_sample.epub"

    try:
        path = make_epub_sample_10pct(args.input, out, include_cover=(not args.no_cover), complete_chapters_only=args.complete_chapters_only)
        print(f"已輸出 EPUB 節錄：{path}")
    except Exception as e:
        print(f"處理失敗：{e}", file=sys.stderr)
        sys.exit(2)

if __name__ == "__main__":
    main()