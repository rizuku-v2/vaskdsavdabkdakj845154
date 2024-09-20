import requests
import feedparser
import pdfkit
import os
import re
import json
import csv
import argparse
from datetime import datetime
from dateutil import parser as date_parser
from jinja2 import Template
from urllib.parse import urljoin, urlparse, urlsplit
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor
from ebooklib import epub
from bs4 import BeautifulSoup

# Initialize a requests session with retry logic for reliability
def create_session_with_retries(retries=3, backoff_factor=0.3):
    session = requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=(500, 502, 504)
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

# Initialize a session with retry logic
session = create_session_with_retries()

# Helper function to save metadata to JSON
def save_metadata(metadata, output_dir, as_json=False):
    if as_json:
        metadata_file = os.path.join(output_dir, "metadata.json")
        with open(metadata_file, "w", encoding="utf-8") as file:
            json.dump(metadata, file, indent=4, ensure_ascii=False)
        print(f"Saved metadata to {metadata_file}")
    else:
        metadata_file = os.path.join(output_dir, "metadata.csv")
        csv_columns = ['title', 'url', 'published_date', 'has_post_body']
        try:
            with open(metadata_file, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=csv_columns)
                writer.writeheader()
                for data in metadata:
                    writer.writerow(data)
            print(f"Saved metadata to {metadata_file}")
        except OSError as e:
            log_error(f"Error saving metadata as CSV: {e}")

# Sanitize filename for file paths
def sanitize_filename(filename):
    return re.sub(r'[\\/*?:"<>|]', "", filename)

# Sanitize URLs for filename use
def sanitize_url(url):
    url_path = urlsplit(url).path
    return sanitize_filename(os.path.basename(url_path))

# Cek apakah situs adalah Blogspot (termasuk custom domain)
def is_blogspot_site(url):
    try:
        # Attempt to access a known Blogspot feed URL pattern
        blogspot_feed_url = url.rstrip('/') + "/feeds/posts/default?alt=rss"
        response = session.head(blogspot_feed_url)
        if response.status_code == 200:
            # Check if the response contains 'xml', a sign of Blogspot feed
            if "xml" in response.headers.get('Content-Type', '').lower():
                print(f"Detected Blogspot site using custom domain: {blogspot_feed_url}")
                return blogspot_feed_url
    except requests.RequestException:
        return None  # Tidak bisa mendeteksi sebagai Blogspot
    return None

# Fungsi baru untuk mendeteksi CMS berdasarkan beberapa karakteristik umum
def detect_cms(url):
    try:
        response = session.get(url)
        html = response.text.lower()

        # Deteksi CMS Blogspot
        if "blogger.com" in html or "blogspot" in url:
            return "blogspot"

        # Deteksi CMS WordPress
        if "wp-content" in html or "wordpress" in html:
            return "wordpress"

        # Tambah deteksi CMS lain jika diperlukan
        # Contoh CMS lainnya bisa termasuk Joomla, Drupal, dll.
        if "joomla" in html:
            return "joomla"
        if "drupal" in html:
            return "drupal"

    except requests.RequestException as e:
        log_error(f"Error detecting CMS: {e}")
    
    return "unknown"  # Jika CMS tidak terdeteksi

# Mencari feed standar untuk non-Blogspot (WordPress, Joomla, Drupal, dll.)
def find_rss_feed(url, cms_type):
    # Feed patterns for common CMS systems
    potential_feeds = ["/feed/", "/rss.xml", "/atom.xml", "/feeds/"]

    # Jika terdeteksi WordPress
    if cms_type == "wordpress":
        potential_feeds = ["/feed/", "/comments/feed/"]

    # Tambahkan deteksi feed untuk CMS lain (misal Joomla, Drupal, dll.)
    elif cms_type == "joomla":
        potential_feeds = ["/index.php?option=com_rss&feed=rss"]

    elif cms_type == "drupal":
        potential_feeds = ["/rss.xml"]

    # Coba cari feed berdasarkan pola feed yang umum
    for feed in potential_feeds:
        feed_url = url.rstrip("/") + feed
        try:
            response = session.head(feed_url)
            if response.status_code == 200:
                print(f"Found RSS feed at: {feed_url}")
                return feed_url
        except requests.RequestException:
            continue
    return None

# Fetch RSS feed and handle pagination for Blogspot feeds
def fetch_rss_feed(url, start_index=None, max_results=500):
    if start_index:
        url = f"{url}&start-index={start_index}&max-results={max_results}"
    print(f"Fetching RSS feed: {url}")
    feed = feedparser.parse(url)
    if feed.bozo:
        log_error(f"Failed to retrieve RSS feed. Error: {feed.bozo_exception}")
        return None
    return feed

# Fetch post content
def fetch_post_content(post_url):
    print(f"Fetching post content from URL: {post_url}")
    response = session.get(post_url)
    if response.status_code != 200:
        log_error(f"Failed to retrieve post content. Status code: {response.status_code} for URL: {post_url}")
        return None

    # Handle encoding correctly and parse the content using BeautifulSoup
    response.encoding = response.apparent_encoding  # Ensure correct encoding is used
    content_type = response.headers.get('Content-Type', '').lower()

    if 'xml' in content_type:
        print("Detected XML content, using XML parser.")
        soup = BeautifulSoup(response.text, features="xml")
    else:
        print("Detected HTML content, using HTML parser.")
        soup = BeautifulSoup(response.text, 'lxml')

    return soup

# Scrape and extract content from a specific div class
def extract_content_by_div(post_content, div_class):
    soup = post_content
    div_content = soup.find('div', class_=div_class)
    if not div_content:
        print(f"No content found in <div class='{div_class}'>")
        return ""
    return div_content

# Download images inside specific <div> elements
def download_images(post_content, post_folder, rss_url, inside_post_body=False):
    soup = post_content

    # Prioritaskan scraping berdasarkan div: post-body, entry-content, post-entry
    if inside_post_body:
        post_body = soup.find('div', class_='post-body') or soup.find('div', class_='entry-content') or soup.find('div', class_='post-entry')

        if post_body:
            images = post_body.find_all('img')
        else:
            images = []
    else:
        images = soup.find_all('img')

    if not images:
        print(f"No images found in the post.")
        return
    
    print(f"Downloading {len(images)} images to {post_folder}")
    for i, img in enumerate(images, start=1):
        img_url = img.get('src')
        if img_url:
            img_url = urljoin(rss_url, img_url)
            try:
                img_data = session.get(img_url).content
                ext = os.path.splitext(img_url)[1]
                if ext.lower() not in ['.jpg', '.jpeg', '.png', '.gif']:  # Filter out non-image files
                    continue
                img_filename = sanitize_url(img_url)
                img_filepath = os.path.join(post_folder, img_filename)
                with open(img_filepath, 'wb') as img_file:
                    img_file.write(img_data)
                print(f"Downloaded {img_filepath}")
            except requests.RequestException as e:
                log_error(f"Error downloading image: {e} from URL: {img_url}")

# Save content as Markdown
def save_as_markdown(post_content, output_path):
    try:
        with open(output_path, 'w', encoding='utf-8') as file:
            file.write(str(post_content))
        print(f"Saved: {output_path}")
    except OSError as e:
        log_error(f"Error saving post as markdown: {e}")

# Save post as a TXT file
def save_as_txt(post_content, output_path):
    try:
        with open(output_path, 'w', encoding='utf-8') as file:
            file.write(str(post_content))
        print(f"Saved: {output_path}")
    except OSError as e:
        log_error(f"Error saving post as txt: {e}")

# Convert post content to PDF with custom CSS for better readability
def convert_to_pdf(post_content, post_title, output_path, images_folder=None):
    print(f"Converting content to PDF: {output_path}")
    
    # Lokasi wkhtmltopdf untuk Windows atau OS lain, sesuaikan dengan sistem yang digunakan
    path_to_wkhtmltopdf = 'C:/Program Files/wkhtmltopdf/bin/wkhtmltopdf.exe'  # Modify as needed for your system
    config = pdfkit.configuration(wkhtmltopdf=path_to_wkhtmltopdf)

    # Jika ada gambar, ganti <img> tag dengan referensi gambar lokal
    if images_folder:
        images = post_content.find_all('img')
        for img in images:
            img_url = img.get('src')
            if img_url:
                img_filename = sanitize_url(img_url)
                img['src'] = os.path.join(images_folder, img_filename)  # Ganti src menjadi path lokal

    # Custom CSS to improve readability on smartphones
    css_style = """
    /* General Text Styling for Readable PDF */
    body {
        font-family: Arial, sans-serif;
        font-size: 14pt;
        line-height: 1.5;
        margin: 20px;
    }
    h1 {
        font-size: 24pt;
        font-weight: bold;
        margin-bottom: 20px;
        text-align: center;
    }
    h2 {
        font-size: 18pt;
        font-weight: bold;
        margin-bottom: 15px;
    }
    h3 {
        font-size: 16pt;
        font-weight: bold;
        margin-bottom: 10px;
    }
    p {
        font-size: 14pt;
        margin-bottom: 10px;
        text-align: justify;
    }
    ul, ol {
        font-size: 14pt;
        margin-bottom: 10px;
    }
    table {
        width: 100%;
        border-collapse: collapse;
        margin-bottom: 20px;
    }
    table, th, td {
        border: 1px solid black;
    }
    th, td {
        padding: 8px;
        text-align: left;
        font-size: 14pt;
    }
    a {
        color: blue;
        text-decoration: none;
        font-size: 14pt;
    }
    blockquote {
        font-size: 14pt;
        margin: 10px 0;
        padding-left: 15px;
        border-left: 4px solid #ccc;
        color: #555;
    }
    img {
        max-width: 100%;
        height: auto;
        display: block;
        margin: 20px auto;
        border-radius: 5px;
    }
    """

    # Generate HTML for PDF with explicit UTF-8 encoding and embedded CSS
    template = Template("""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta http-equiv="X-UA-Compatible" content="IE=edge">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{{ title }}</title>
        <style>{{ css }}</style>
    </head>
    <body>
        <h1>{{ title }}</h1>
        <div>{{ content|safe }}</div>
    </body>
    </html>
    """)
    
    html_content = template.render(title=post_title, content=str(post_content), css=css_style)

    temp_html_path = output_path.replace('.pdf', '.html')
    
    # Menyimpan HTML ke file sementara dengan encoding utf-8
    with open(temp_html_path, 'w', encoding='utf-8') as f:
        f.write(html_content)

    try:
        # Set wkhtmltopdf options to enforce UTF-8 rendering
        options = {
            'encoding': "UTF-8",  # Explicitly tell wkhtmltopdf to treat input as UTF-8
        }
        
        # Biarkan wkhtmltopdf menentukan encoding dan mengonversi HTML ke PDF
        pdfkit.from_file(temp_html_path, output_path, configuration=config, options=options)
    except OSError as e:
        log_error(f"Error converting post to PDF: {e} for file: {output_path}")

    # Menghapus file sementara HTML setelah proses selesai
    if os.path.exists(temp_html_path):
        os.remove(temp_html_path)

# Save post as EPUB
def save_as_epub(post_content, post_title, output_path, images_folder=None):
    book = epub.EpubBook()

    # Set metadata
    book.set_identifier(post_title)
    book.set_title(post_title)
    book.set_language('en')

    # Add a chapter
    chapter = epub.EpubHtml(title=post_title, file_name='chap.xhtml', lang='en')
    
    # If there are images, replace <img> tag with local image links
    if images_folder:
        images = post_content.find_all('img')
        for i, img in enumerate(images, start=1):
            img_url = img.get('src')
            if img_url:
                img_filename = sanitize_url(img_url)
                local_img_path = os.path.join(images_folder, img_filename)
                # Add image to EPUB book
                img_path_in_epub = f"images/{img_filename}"
                img['src'] = img_path_in_epub
                
                # Add image to EPUB manifest
                with open(local_img_path, 'rb') as img_file:
                    image_data = img_file.read()
                img_item = epub.EpubItem(uid=f"img_{i}", file_name=img_path_in_epub, media_type='image/jpeg', content=image_data)
                book.add_item(img_item)

    chapter.content = f"<h1>{post_title}</h1><div>{str(post_content)}</div>"
    book.add_item(chapter)

    # Define Table of Contents
    book.toc = (epub.Link(chapter.file_name, post_title, 'chap_1'),)

    # Add default NCX and Nav file
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    # Define Spine
    book.spine = ['nav', chapter]

    # Save the epub file
    epub.write_epub(output_path, book)
    print(f"Saved: {output_path}")

# Function to log errors
def log_error(error_message):
    with open("error_log.txt", "a") as log_file:
        log_file.write(error_message + "\n")

# Get website name from URL
def get_website_name(url):
    return urlparse(url).netloc.replace('www.', '')

# Function to fetch labels/tags from the RSS feed or from the website's HTML
def fetch_labels_or_tags(rss_url):
    print("Fetching available labels/tags from the website...")
    response = session.get(rss_url)
    soup = BeautifulSoup(response.text, 'lxml')

    # Try to find tags in <category> elements from the feed if available
    categories = set()
    for category in soup.find_all('category'):
        categories.add(category.text.strip())

    # If no categories found, try searching for tags in the page (assuming certain class names)
    if not categories:
        # Check for a common tag element in Blogspot or WordPress sites (e.g., in a sidebar or footer)
        possible_tag_classes = ['label', 'tag', 'category']
        for tag_class in possible_tag_classes:
            tag_elements = soup.find_all('a', class_=tag_class)
            for tag in tag_elements:
                categories.add(tag.text.strip())

    # Return list of unique tags, sorted alphabetically
    return sorted(list(categories))

# Function to scrape based on the selected label/tag
def fetch_posts_by_label(rss_url, label, output_dir, mode, download_images_separately=False, debug=False):
    print(f"Scraping posts with label: {label}")

    # Modify RSS feed URL to fetch posts based on the selected label (this works for Blogspot)
    if "blogspot.com" in rss_url:
        # Correct URL format for Blogspot feeds based on label
        label_url = rss_url.split("/feeds/posts/default")[0] + f"/feeds/posts/default/-/{label.replace(' ', '%20')}?alt=rss"
    else:
        # Fix for custom domain Blogspot - do not include /label/ in the URL
        if "feeds/posts/default" in rss_url:
            # Split the URL properly and avoid duplicating ?alt=rss
            rss_url_base = rss_url.split('?')[0]  # Get base RSS URL without parameters
            # Create the correct URL for Blogspot custom domain, dynamically insert the label
            label_url = rss_url_base + f"/-/"+ label.replace(' ', '%20') + "?alt=rss"
        else:
            # For non-Blogspot, attempt to use search/label
            label_url = rss_url + f"/search/label/{label.replace(' ', '%20')}?alt=rss"

    print(f"Fetching RSS feed: {label_url}")

    # Scrape and save posts for the selected label
    scrape_and_save_rss_posts(label_url, output_dir, mode, download_images_separately, debug)

# Process each post and download the content
def process_post(entry, post_counter, output_dir, mode, metadata, download_images_separately, rss_url):
    post_url = entry.link
    post_content = fetch_post_content(post_url)
    
    if post_content is None:
        return

    post_title = sanitize_filename(entry.title)
    post_date = date_parser.parse(entry.published) if 'published' in entry else datetime.now()

    # Cek apakah ada <div class="post-body">, <div class="entry-content">, <div class="post-entry">
    website_name = get_website_name(post_url)
    is_blogspot = is_blogspot_site(post_url) is not None  # Check if the site is Blogspot

    website_folder = os.path.join(output_dir, website_name)
    if not os.path.exists(website_folder):
        os.makedirs(website_folder)

    post_folder = os.path.join(website_folder, f"{post_counter} - {post_title}")
    if not os.path.exists(post_folder):
        os.makedirs(post_folder)

    # Scrape based on div class only if the site is Blogspot and in specific modes (PDF, EPUB)
    if is_blogspot:
        post_body_div = post_content.find('div', class_='post-body') or post_content.find('div', class_='entry-content') or post_content.find('div', class_='post-entry')
        has_post_body = bool(post_body_div)
    else:
        # Fallback to scraping the entire content if not Blogspot or no specific div found
        post_body_div = post_content
        has_post_body = True

    images_folder = None
    if download_images_separately:
        images_folder = os.path.join(post_folder, "images")
        if not os.path.exists(images_folder):
            os.makedirs(images_folder)

        # If blogspot and div exists, download images only from that div
        if is_blogspot and has_post_body:
            download_images(post_body_div, images_folder, rss_url, inside_post_body=True)
        else:
            # Otherwise, download all images from the post
            download_images(post_content, images_folder, rss_url, inside_post_body=False)

    # Extract content based on the div class (similar to image download)
    div_content = extract_content_by_div(post_content, "post-body")

    if mode == "PDF":
        output_file = os.path.join(post_folder, f"{post_title}.pdf")
        convert_to_pdf(div_content if has_post_body else post_content, post_title, output_file, images_folder)
    elif mode == "TXT":
        output_file = os.path.join(post_folder, f"{post_title}.txt")
        save_as_txt(div_content if has_post_body else post_content, output_file)
    elif mode == "MD":
        output_file = os.path.join(post_folder, f"{post_title}.md")
        save_as_markdown(div_content if has_post_body else post_content, output_file)
    elif mode == "EPUB":
        output_file = os.path.join(post_folder, f"{post_title}.epub")
        save_as_epub(div_content if has_post_body else post_content, post_title, output_file, images_folder)

    # Simpan metadata
    metadata.append({
        'title': entry.title,
        'url': post_url,
        'published_date': post_date.isoformat(),
        'has_post_body': has_post_body
    })

# Main function to determine whether the site is Blogspot or another CMS and scrape accordingly
def determine_rss_feed_url(rss_url):
    # Cek apakah ini Blogspot (termasuk custom domain Blogspot)
    blogspot_feed_url = is_blogspot_site(rss_url)
    if blogspot_feed_url:
        return blogspot_feed_url

    # Deteksi CMS lainnya (WordPress, Joomla, Drupal, dll.)
    cms_type = detect_cms(rss_url)
    print(f"Detected CMS: {cms_type}")

    # Jika terdeteksi CMS WordPress atau lainnya, cari feed yang sesuai
    if cms_type != "blogspot":
        non_blogspot_feed_url = find_rss_feed(rss_url, cms_type)
        if non_blogspot_feed_url:
            return non_blogspot_feed_url

    # Jika tidak menemukan feed, kembalikan URL asli
    return rss_url

# Main function to scrape RSS and save posts
def scrape_and_save_rss_posts(rss_url, output_dir, mode, download_images_separately=False, debug=False):
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # Tentukan RSS feed URL yang sesuai (baik Blogspot atau CMS lainnya)
    rss_url = determine_rss_feed_url(rss_url)

    post_counter = 1
    metadata = []
    start_index = 1
    max_results = 500

    while True:
        feed = fetch_rss_feed(rss_url, start_index=start_index, max_results=max_results)
        if feed is None or len(feed.entries) == 0:
            print("No more posts to scrape.")
            break

        entries = feed.entries

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = []
            for entry in entries:
                futures.append(executor.submit(process_post, entry, post_counter, output_dir, mode, metadata, download_images_separately, rss_url))
                post_counter += 1

            for future in futures:
                future.result()

        start_index += len(entries)

        if debug:
            save_metadata(metadata, output_dir)

    save_metadata(metadata, output_dir, as_json=True)  # Save metadata to JSON as well

# Main function to include the option to search by label or tag
def main():
    parser = argparse.ArgumentParser(
        description='Scrape RSS feed and save posts in various formats. '
                    'This script supports scraping from Blogspot, WordPress, and other CMS-based websites. '
                    'You can output posts as PDF, TXT, Markdown (MD), or EPUB, and optionally download images. '
                    'The program automatically detects available labels/tags and allows scraping based on them.'
    )
    
    parser.add_argument(
        'rss_url', nargs='?', 
        help='The URL of the RSS feed to scrape. Works with Blogspot (including custom domains), WordPress, and other CMS.'
    )
    
    parser.add_argument(
        '--output-dir', default='downloaded_posts', 
        help='Directory where posts will be saved. Default is "downloaded_posts".'
    )
    
    parser.add_argument(
        '--mode', choices=['PDF', 'TXT', 'MD', 'EPUB'], 
        help='The format in which to save the posts. Options include:\n'
             '  PDF   - Save posts as PDF files.\n'
             '  TXT   - Save posts as plain text files.\n'
             '  MD    - Save posts as Markdown files.\n'
             '  EPUB  - Save posts as EPUB e-books.\n'
    )
    
    parser.add_argument(
        '--download-images', action='store_true', 
        help='Download images separately and save them in a subfolder for each post. '
             'This option is especially useful when saving in PDF or EPUB formats, where images can be included in the document.'
    )
    
    parser.add_argument(
        '--debug', action='store_true', 
        help='Enable debug mode for detailed logging. This will print additional information for troubleshooting and save metadata in JSON format.'
    )
    
    args = parser.parse_args()

    # Jika tidak ada RSS URL diberikan, tampilkan bantuan
    if not args.rss_url:
        parser.print_help()
        return

    # Jika mode tidak diberikan, tampilkan pesan khusus
    if not args.mode:
        print("\n[ERROR] Tolong masukan --mode\n")
        print("Penjelasan Mode:")
        print("  --mode menentukan format output untuk menyimpan hasil scrape.")
        print("  Mode yang tersedia:")
        print("    PDF   - Menyimpan posting sebagai file PDF.")
        print("    TXT   - Menyimpan posting sebagai file teks biasa.")
        print("    MD    - Menyimpan posting sebagai file Markdown.")
        print("    EPUB  - Menyimpan posting sebagai file EPUB (e-book format).")
        print("\nContoh penggunaan:")
        print("  python3 scraper.py https://example.com/rss --mode PDF\n")
        return

    # Logika pencarian berdasarkan label/tag atau scrape semua
    search_by_label = input("Do you want to search by label/tag? (y/n): ").strip().lower()

    if search_by_label == 'y':
        # Fetch available labels/tags
        labels = fetch_labels_or_tags(args.rss_url)
        if not labels:
            print("No labels or tags found on the website.")
            return

        # Display all available labels/tags, sorted alphabetically
        print("Available labels/tags:")
        for idx, label in enumerate(labels, 1):
            print(f"{idx}. {label}")

        # Let the user select a label by number
        selected_label_idx = int(input("Select a label by number: "))
        selected_label = labels[selected_label_idx - 1]
        
        # Scrape posts based on the selected label
        fetch_posts_by_label(args.rss_url, selected_label, args.output_dir, args.mode, args.download_images, args.debug)
    else:
        # Scrape all posts as usual
        scrape_and_save_rss_posts(args.rss_url, args.output_dir, args.mode, args.download_images, args.debug)

if __name__ == "__main__":
    main()
