#!/usr/bin/env python
# coding: utf-8

import json
import re
import pathlib
import shutil
import copy
import posixpath
import itertools
import math

import frontmatter
import pendulum
import toml

from wand.image import Image as WandImage

import markdown
from markdown.treeprocessors import Treeprocessor
from markdown.extensions import Extension

import markupsafe
from jinja2 import Environment, FileSystemLoader

from slugify import slugify
from typographeur import typographeur

from tqdm import tqdm


class ImageFolder:
    def __init__(self,
                 output_folder,
                 assets_folder,
                 max_img_size = None,
                 max_file_size = None,
                 max_thumb_size=(400, 300)
                ):
        self.output_folder = output_folder
        self.assets_folder = assets_folder
        self.max_img_size = max_img_size
        self.max_file_size = max_file_size
        self.max_thumb_size = max_thumb_size
        self.images = {}
        self.thumbs = {}
        self.counters = {}
    
    @classmethod
    def make(cls,
             metadatafile,
             output_folder,
             assets_folder,
             max_img_size = None,
             max_file_size = None,
             max_thumb_size=(400, 300)
            ):
        self = cls(output_folder, assets_folder, max_img_size, max_file_size, max_thumb_size)
        with open(metadatafile, "r", encoding="utf-8") as fin:
            metadata = json.load(fin)
            self.image_bank = {}
            for data in metadata["matches"]:
                path_matched = data["path_matched"] or data["path_target"]
                p = pathlib.Path(data["path_target"])
                index = str(pathlib.Path(*p.parts[-2:]))
                self.image_bank[index] = path_matched
        return self
    
    def match(self, path, folder):
        p = pathlib.Path(path).parts[-2:]
        index = "/".join(p)
        matched = self.image_bank.get(index)
        source = matched if matched else path
        if source in self.images:
            destination = self.images[source]
        else:
            directory = self.output_folder / folder
            if (self.max_file_size is None) and (self.max_img_size is not None):
                new_name = pathlib.Path(source).name
            else:
                if directory not in self.counters:
                    self.counters[directory] = itertools.count(1)
                new_name = "{:03d}.jpg".format(next(self.counters[directory]))
            destination = directory / new_name
            if not matched:
                print("Warning image {} not found".format(index))
            else:
                self.images[source] = destination
        return destination

    def thumb(self, path, name):
        p = pathlib.Path(path).parts[-2:]
        index = "/".join(p)
        matched = self.image_bank.get(index)
        source = matched if matched else path
        if source in self.thumbs:
            destination = self.images[source]
        else:
            relpath = pathlib.PurePosixPath("img") / "{}.jpg".format(name)
            destination = self.assets_folder / relpath
            if not matched:
                print("Warning image {} not found".format(index))
            else:
                self.thumbs[source] = destination
        return relpath.as_posix()
    
    def do_copy(self):
        if (self.max_file_size is None) and (self.max_img_size is not None):
            self._do_copy_vanilla()
        else:
            self._do_copy_convert()
        self._do_copy_thumbs()
    
    def _do_copy_vanilla(self):
        for src, dst in tqdm(self.images.items(), desc="Copying images"):
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            
    
    def _do_copy_convert(self):
        for src, dst in tqdm(self.images.items(), desc="Converting images"):
            self._do_convert(src, dst, self.max_file_size, self.max_img_size)
    
    def _do_copy_thumbs(self):
        for src, dst in tqdm(self.thumbs.items(), desc="Creating thumbs"):
            self._do_convert(src, dst, None, self.max_thumb_size)
        
    def _do_convert(self, src, dst, max_file_size, max_img_size):
        dst.parent.mkdir(parents=True, exist_ok=True)
        with WandImage(filename=src) as img_src:
            with img_src.clone() as img_dst:
                img_dst.format = 'jpeg'
                if max_file_size is not None:
                    img_dst.options['jpeg:extent'] = max_file_size
                if max_img_size is not None:
                    w, h = img_dst.size
                    mw, mh = max_img_size
                    rww, rhw = mw, int(h * mw / w)
                    rwh, rhh = int(w * mh / h), mh
                    rw, rh = min(w, rww, rwh), min(h, rhw, rhh)
                    if (rw < w) or (rh < h):
                        img_dst.resize(rw, rh)
                img_dst.save(filename=dst)

   
class ImageProcessor(Treeprocessor):
    def __init__(self, folder_name, folder, context):
        self.folder_name = folder_name
        self.folder = folder
        self.context = context
    
    def run(self, root):
        for img_tag in root.iter("img"):
            src = img_tag.attrib.get("src")
            if not src:
                print("EMPTY TAG")
                continue
            new_src = self.folder.match(src, self.folder_name)
            new_src_url = self.context.url_for_abs(new_src)
            img_tag.attrib["src"] = new_src_url


class ImageProcessorExtension(Extension):
    def __init__(self, processor):
        self.processor = processor
    def extendMarkdown(self, md):
        md.treeprocessors.register(self.processor, 'imgproc', 5)


class PostContext:
    def __init__(self, output_root, assets_root, filepath):
        self.output_root = pathlib.PurePosixPath(output_root)
        self.assets_root = pathlib.PurePosixPath(assets_root)
        self.filepath = pathlib.PurePosixPath(filepath)
        
    def url_for_abs(self, abspath):
        return posixpath.relpath(abspath, self.filepath.parent)
    
    def url_for(self, url):
        url_with_root = self.output_root / pathlib.PurePosixPath(url.lstrip("/"))
        return posixpath.relpath(url_with_root, self.filepath.parent)
    
    def url_for_assets(self, url):
        url_with_root = self.assets_root / pathlib.PurePosixPath(url.lstrip("/"))
        return posixpath.relpath(url_with_root, self.filepath.parent)


class TplEnvironment(Environment):
    def __init__(self, template_path, post_context):
        super().__init__(
            loader=FileSystemLoader(template_path),
            autoescape=True
        )
        self.post_context = post_context
        self.globals["url_for"] = self.url_for
        self.globals["url_for_assets"] = self.url_for_assets
    
    def url_for(self, url):
        return self.post_context.url_for(url)
    
    def url_for_assets(self, url):
        return self.post_context.url_for_assets(url)


class Post:
    def __init__(self, post, prev=None, env=None):
        self.post = post
        self.prev = prev
        self.env = env
    def write(self):
        date = self.post.get('date_event')
        if not date:
            date = self.post.get('date').strftime("%Y-%m-%d") if self.post.get('date') else None
        post = frontmatter.load(self.post.get('source'))
        context = self.env.make_context(self.post.get('target'))
        folder_name = "{}-{}".format(date, slugify(self.post.get('title')))
        content = markdown.markdown(
            post.content,
            extensions=[
                ImageProcessorExtension(
                    ImageProcessor(
                        folder_name,
                        self.env.folder,
                        context
                    )
                )
            ]
        )
        typo_content = typographeur(content)
        tpl = self.env.make_tpl_env(context)
        single_tpl = tpl.get_template(self.env.SINGLE_TPL)
        # Shallow copy "post" so that "content" is not added to bucket
        data = {
            "post": copy.copy(self.post),
            "prev": self.prev,
            "site": self.env.get_config()
        }
        data["post"]["content"] = markupsafe.Markup(typo_content)
        self.post.get('target').parent.mkdir(parents=True, exist_ok=True)
        with open(self.post.get('target'), "w", encoding="utf-8") as fout:
            fout.write(single_tpl.render(**data))


class PostBucket:
    def __init__(self, site_env):
        self.site_env = site_env
    
    @classmethod
    def make(cls, site_env):
        self = cls(site_env)
        content_path = self.site_env.path("content")
        self._posts = []
        for filepath in content_path.rglob("*.md"):
            try:
                post_meta = frontmatter.load(filepath)
            except Exception as err:
                raise RuntimeError(str(filepath)) from err
            post_dict = self._reshape_post_meta(post_meta)
            post_dict["source"] = filepath
            p_target = self.site_env.path("posts") / (filepath.parent.name + ".html")
            post_dict["target"] = p_target
            post_dict["url"] = p_target.relative_to(self.site_env.path("output")).as_posix()
            feat_img = post_dict["resources"].get("featuredImage")
            if feat_img:
                src = feat_img.get('src', '')
                if src != '':
                    new_src = self.site_env.folder.thumb(src, slugify(post_dict['title']))
                    feat_img['src'] = new_src
            self._posts.append(post_dict)
        self._posts.sort(key=lambda x:x["date"], reverse=True)
        return self
    
    @staticmethod
    def _reshape_post_meta(post_meta):
        post_dict = {
            key: copy.deepcopy(post_meta.get(key))
            for key in post_meta.keys()
            if key not in ["resources", "date"]
        }
        post_dict["date"] = pendulum.parse(post_meta['date'])
        post_dict["resources"] = {}
        for resource in post_meta.get("resources", []):
            post_dict["resources"][resource['name']] = copy.deepcopy(resource)
        return post_dict
    
    def __iter__(self):
        posts, prevs = itertools.tee(self._posts, 2)
        next(prevs, None)
        for post, prev in itertools.zip_longest(posts, prevs):
            yield Post(post, prev, env=self.site_env)
    
    def __len__(self):
        return len(self._posts)


class SiteEnvironment:
    CONFIG_FILE = "config_site.toml"
    SITE_FOLDER = "site"
    SINGLE_TPL = "single.html.j2"
    INDEX_TPL = "index.html.j2"
    
    def __init__(self, root):
        self.root = pathlib.Path(root).resolve()
        self._read_config()
        self._make_image_folder()
    
    def _read_config(self):
        p_config = self.root / self.CONFIG_FILE
        with open(p_config, 'r', encoding="utf-8") as fin:
            self._config = toml.load(fin)
        self._make_paths()
    
    def _make_paths(self):
        self._path = {}
        self._path["root"] = self.root
        self._path["content"] = (self.root / self.config("paths.content", "content")).resolve()
        self._path["template"] = (self.root / self.config("paths.template")).resolve()
        self._path["output"] = (self.root / self.config("paths.output", "output")).resolve()
        self._path["photos"] = (self._path["output"] / self.config("photos.path", "photos")).resolve()
        self._path["site"] = self._path["output"] / self.SITE_FOLDER
        self._path["posts"] = self._path["site"] / "posts"
        self._path["assets"] = self._path["site"] / "assets"
        self._path["metadata"] = (self.root / self.config("photos.metadata")).resolve()
    
    def _make_image_folder(self):
        if self.config("photos.highres"):
            max_img_size = None
            max_file_size = None
        else:
            max_img_size = self.config("photos.max_img_size", None)
            max_file_size = self.config("photos.max_file_size", None)
            max_thumb_size = self.config("photos.max_thumb_size", (400, 300))
        self.folder = ImageFolder.make(
            self.path("metadata"),
            self.path("photos"),
            self.path("assets"),
            max_img_size,
            max_file_size,
            max_thumb_size
        )
    
    def config(self, key, default=KeyError):
        base = self._config
        for k in key.split("."):
            base = base.get(k)
            if base is None:
                if default.__class__ == type and issubclass(default, Exception):
                    raise default(key)
                else:
                    return default
        else:
            return base
    
    def path(self, key):
        return self._path[key]
    
    def get_config(self):
        return self._config
    
    def make_context(self, filepath):
        return PostContext(
            self.path("output"),
            self.path("assets"),
            filepath
        )
    
    def make_tpl_env(self, context):
        return TplEnvironment(
            self.path("template") / "layouts",
            context
        )
    
    def get_post_bucket(self):
        return PostBucket.make(self)


class PostIndex:
    def __init__(self, env):
        self.env = env
        
    def _post_index_path(self, nb_pages):
        yield self.env.path("output") / self.env.config("home.url")
        for i in range(nb_pages-1):
            yield self.env.path("posts") / "page" / (str(i+2) + ".html")
    def _index_pagination(self, nb_pages):
        def make_pagination(n, p):
            return {
                "next": ({"url": n.relative_to(self.env.path("output")).as_posix()} if n else None),
                "prev": ({"url": p.relative_to(self.env.path("output")).as_posix()} if p else None)
            }
        nxt, curr, prev = itertools.tee(self._post_index_path(nb_pages), 3)
        next(prev, None)
        yield next(curr), make_pagination(None, next(prev, None))
        for i in range(nb_pages-1):
            yield next(curr), make_pagination(next(nxt), next(prev, None))
    
    def write(self, bucket):
        posts_per_page =self.env.config("posts_per_page", 8)
        nb_pages = math.ceil(len(bucket) / posts_per_page)
        postit = iter(bucket)
        for filepath, paginator in self._index_pagination(nb_pages):
            data = {
                "site": self.env.get_config(),
                "paginator": paginator,
                "posts": [post.post for post in itertools.islice(postit, 8)]
            }
            ctx = self.env.make_context(filepath)
            tpl = self.env.make_tpl_env(ctx)
            index_tpl = tpl.get_template(self.env.INDEX_TPL)
            filepath.parent.mkdir(parents=True, exist_ok=True)
            with open(filepath, "w", encoding="utf-8") as fout:
                fout.write(index_tpl.render(**data))


def main():
    import sys
    import os.path

    if len(sys.argv) != 2:
        print("Usage: {} path/to/site".format(sys.argv[0]))
        sys.exit(-1)

    path = os.path.abspath(sys.argv[1])

    env = SiteEnvironment(path)

    bucket = env.get_post_bucket()

    for post in tqdm(bucket, desc="Writing posts"):
        post.write()

    pidx = PostIndex(env)
    pidx.write(bucket)

    env.folder.do_copy()


if __name__ == "__main__":
    main()
