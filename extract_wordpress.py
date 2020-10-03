#!/usr/bin/env python
# coding: utf-8

import os
import os.path
import shutil
import re
import pathlib

from collections import namedtuple
from urllib.parse import urlparse
from datetime import datetime

import requests
from bs4 import BeautifulSoup, Comment
from slugify import slugify
from pytz import timezone

import sqlalchemy
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from PIL import Image

import markdownify as md
import frontmatter
import toml

from photo_match import ImageInfos, ImageMatcher, load_bucket

def make_tables(wp_id, engine):
    Base = declarative_base()

    class WpPost(Base):
        __table__ = sqlalchemy.Table(
            'wp_{}_posts'.format(wp_id),
            Base.metadata,
            autoload=True,
            autoload_with=engine
        )

    class WpOption(Base):
        __table__ = sqlalchemy.Table(
            'wp_{}_options'.format(wp_id),
            Base.metadata,
            autoload=True,
            autoload_with=engine
        )

    return (WpPost, WpOption)

Post = namedtuple(
    'Post',
    [
        'post_name',
        'post_title',
        'post_date',
        'post_content',
        'post_date_from_images'
    ]
)

class ImageProcessor:
    def __init__(self, path):
        self.path = path
        self.session = requests.Session()
        self.imgs = []
    
    def add_urls(self, urls):
        dates = []
        for url, relpath in urls:
            filepath = os.path.abspath(os.path.join(self.path, relpath[1:]))
            self.imgs.append((url, filepath))
            if os.path.exists(filepath):
                image = Image.open(filepath)
            else:
                image = None
            date = ImageInfos.guess_date(pathlib.Path(filepath), image)
            dates.append(date)
        return dates
    
    def save_file(self, url, filepath):
        if os.path.exists(filepath):
            return
        res = self.session.get(url, stream=True)
        if res.status_code == 200:
            res.raw.decode_content = True
            with open(filepath, "wb") as f:
                shutil.copyfileobj(res.raw, f)
        else:
            print("Could not access", url)
    
    def download(self):
        for url, filepath in self.imgs:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            self.save_file(url, filepath)

class PostProcessor:
    def __init__(self, path, photodir):
        self.path = path
        self.photodir = photodir
        self.img_processor = ImageProcessor(path)
        self.timezone = "UTC"
    
    def set_timezone(self, timezone_):
        self.timezone = timezone_
    
    def process_posts(self, posts):
        for post in posts:
            ppost = self.process_post(post)
            if ppost is not None:
                yield ppost
    
    def process_post(self, post):
        if self.filter_post(post):
            return None
        
        name = self.process_name(post)
        str_date = self.process_date(post)
        
        relpath = os.path.join(self.photodir, name)
        html_abspath = os.path.join("/", relpath)
        
        content, img_urls = self.process_content(
            post.post_content,
            html_abspath
        )
        dates = self.img_processor.add_urls(img_urls)
        date_from_images = self.process_date_from_images(dates)
        
        return Post(name, post.post_title, str_date, content, date_from_images)
    
    def process_date_from_images(self, dates):
        nonone = [date for date in dates if date is not None]
        if len(nonone) == 0: return None
        best_date = max(nonone)
        if best_date is None: return None
        best_date = best_date.strftime("%Y-%m-%d")
        return best_date

    def filter_post(self, post):
        if post.post_status == "auto-draft":
            return True
        if post.post_status == "trash":
            if post.post_modified_gmt < datetime(2020, 7, 29, 8, 16, 26):
                return True
        return False
    
    def process_name(self, post):
        name = post.post_name
        if post.post_status == "trash":
            name = slugify(post.post_title)
        return name
    
    def process_date(self, post):
        utc_date = post.post_modified_gmt.replace(tzinfo=timezone('UTC'))
        wp_date = utc_date.astimezone(timezone(self.timezone))
        return wp_date.isoformat()
    
    def process_content(self, content, img_dir_path):
        soup = BeautifulSoup(content, 'html.parser')
        
        img_urls = []
        
        for img_tag in soup.find_all('img'):
            src = img_tag.get("src")
            if src:
                filename = urlparse(src).path.split("/")[-1]
                new_src = os.path.join(img_dir_path, filename)
                img_urls.append((src, new_src))
                img_tag["src"] = new_src
            if img_tag.parent.name == "a":
                img_tag.parent.unwrap()
        
        for fig_tag in soup.find_all("figure"):
            if not isinstance(fig_tag.parent, BeautifulSoup):
                fig_tag.parent.unwrap()
            for cap_tag in fig_tag.find_all("figcaption"):
                cap_tag.name = "p"
            fig_tag.name = "p"
        
        is_comment = lambda tag:isinstance(tag, Comment)
        for comment_tag in soup.find_all(string=is_comment):
            comment_tag.extract()
        
        soup.smooth()
        converter = md.MarkdownConverter()
        content = converter.process_tag(soup)
        
        return content, img_urls

class WordpressExtractor:
    CONFIG_FILE = "config_site.toml"
    OUTPUT_FOLDER = "static"
    CONTENT_FOLDER = "content"
    POSTS_FOLDER = "posts"

    def __init__(self, path):
        self.path = path
        self._load_config(path)
        self.proc = PostProcessor(
            os.path.join(self.path, self.OUTPUT_FOLDER),
            self.photodir
        )

    def _load_config(self, path):
        configpath = os.path.join(path, self.CONFIG_FILE)
        with open(configpath, 'r', encoding="utf-8") as fin:
            self.config = toml.load(fin)
        self.sqlurl = self.config.get("extract", {}).get("database", "")
        self.wp_id = self.config.get("extract", {}).get("site_id")
        self.download = self.config.get("photos", {}).get("download", True)
        self.photodir = self.config.get("photos", {}).get("path", "photos")

    def write_post(self, post):
        metadata = {
            'title': post.post_title,
            'date': post.post_date,
            'date_event': post.post_date_from_images,
            'description': "",
            'categories': [],
            'toc': False,
            'dropCap': True,
            'displayInMenu': False,
            'displayInList': True,
            'draft': False,
            'resources': [
                {
                    'name': 'featuredImage',
                    'src': '',
                    'params': {'description': 'description'}
                }
            ]
        }
        post_md = frontmatter.Post(
            post.post_content,
            frontmatter.default_handlers.YAMLHandler(),
            **metadata
        )
        
        filepath = os.path.join(
            self.path,
            self.CONTENT_FOLDER,
            self.POSTS_FOLDER,
            post.post_name,
            "index.md"
        )
        
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        
        with open(filepath, "wb") as fout:
            frontmatter.dump(post_md, fout, encoding='utf-8')
    
    def process_all(self):
        engine = sqlalchemy.create_engine(self.sqlurl, echo=False)
        WpPost, WpOption = make_tables(self.wp_id, engine)

        Session = sessionmaker(bind=engine)
        session = Session()

        wordpress_timezone = (
            session
            .query(WpOption.option_value)
            .filter(WpOption.option_name == "timezone_string")
            .scalar()
        )

        self.proc.set_timezone(wordpress_timezone)

        query = session.query(WpPost).filter(WpPost.post_type == "post")

        for post in self.proc.process_posts(query):
            self.write_post(post)
        
        if self.download:
            self.proc.img_processor.download()


def main():
    import sys

    if len(sys.argv) != 2:
        print("Usage: {} path/to/site".format(sys.argv[0]))
        sys.exit(-1)
    
    wpe = WordpressExtractor(os.path.abspath(sys.argv[1]))
    wpe.process_all()

if __name__ == "__main__":
    main()