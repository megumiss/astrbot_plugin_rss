import os
import json
from urllib.parse import urlparse
from lxml import etree
from bs4 import BeautifulSoup
import re

class DataHandler:
    def __init__(self, config_path="data/astrbot_plugin_rss_data.json", default_config=None):
        self.config_path = config_path
        self.default_config = default_config or {
            "rsshub_endpoints": []
        }
        self.data = self.load_data()

    def get_subs_channel_url(self, user_id) -> list:
        """获取用户订阅的频道 url 列表"""
        subs_url = []
        for url, info in self.data.items():
            if url == "rsshub_endpoints" or url == "settings":
                continue
            if user_id in info["subscribers"]:
                subs_url.append(url)
        return subs_url

    def load_data(self):
        """从数据文件中加载数据"""
        if not os.path.exists(self.config_path):
            with open(self.config_path, "w", encoding="utf-8") as f:
                f.write(json.dumps(self.default_config, indent=2, ensure_ascii=False))
        with open(self.config_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_data(self):
        """保存数据到数据文件"""
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def parse_channel_text_info(self, text):
        """解析RSS频道信息"""
        root = etree.fromstring(text)
        
        # 安全获取title
        title_elements = root.xpath("//channel/title")
        if not title_elements:
            title_elements = root.xpath("//title")
        title = title_elements[0].text if title_elements and title_elements[0].text else "未知频道"
        
        # 安全获取description
        desc_elements = root.xpath("//channel/description")
        if not desc_elements:
            desc_elements = root.xpath("//description")
        description = desc_elements[0].text if desc_elements and desc_elements[0].text else "无描述"
        
        return title, description

    def strip_html_pic(self, html)-> list[str]:
        """解析HTML内容，提取图片地址
        """
        if not html:
            return []
        
        soup = BeautifulSoup(html, "html.parser")
        ordered_content = []

        # 处理图片节点
        for img in soup.find_all('img'):
            img_src = img.get('src') or img.get('data-src')  # 支持懒加载图片
            if img_src:
                # 过滤掉tracking pixels和emoji
                if not any(x in img_src.lower() for x in ['tracking', 'pixel', 'emoji', 'icon']) and \
                   not img_src.endswith('.gif') or 'github' in img_src:
                    ordered_content.append(img_src)

        return ordered_content
    
    def extract_media_urls(self, html) -> dict:
        """提取HTML中的媒体URL(图片、视频等)"""
        if not html:
            return {"images": [], "videos": [], "audios": []}
        
        soup = BeautifulSoup(html, "html.parser")
        media = {"images": [], "videos": [], "audios": []}
        
        # 提取图片
        for img in soup.find_all('img'):
            src = img.get('src') or img.get('data-src')
            if src and not any(x in src.lower() for x in ['tracking', 'pixel']):
                media["images"].append(src)
        
        # 提取视频
        for video in soup.find_all(['video', 'iframe']):
            src = video.get('src') or (video.find('source') and video.find('source').get('src'))
            if src:
                media["videos"].append(src)
        
        # 提取音频
        for audio in soup.find_all('audio'):
            src = audio.get('src') or (audio.find('source') and audio.find('source').get('src'))
            if src:
                media["audios"].append(src)
        
        return media

    def strip_html(self, html):
        """去除HTML标签,保留基本格式"""
        if not html:
            return ""
        
        soup = BeautifulSoup(html, "html.parser")
        
        # 移除script和style标签
        for tag in soup(['script', 'style', 'iframe']):
            tag.decompose()
        
        # 处理段落和换行
        for br in soup.find_all('br'):
            br.replace_with('\n')
        for p in soup.find_all('p'):
            p.append('\n')
        
        # 获取文本
        text = soup.get_text(separator=' ')
        
        # 清理多余空白
        text = re.sub(r' +', ' ', text)  # 多个空格变一个
        text = re.sub(r'\n\s*\n', '\n\n', text)  # 多个换行变两个
        text = text.strip()
        
        return text
    
    def smart_truncate(self, text: str, max_length: int, preserve_words: bool = True) -> str:
        """智能截断文本,尽量在句子或词语边界截断"""
        if len(text) <= max_length:
            return text
        
        if preserve_words:
            # 尝试在句子边界截断
            truncated = text[:max_length]
            # 查找最后一个句号、问号或感叹号
            for delimiter in ['。', '！', '？', '.', '!', '?', '\n']:
                last_pos = truncated.rfind(delimiter)
                if last_pos > max_length * 0.7:  # 至少保留70%
                    return text[:last_pos + 1]
            
            # 如果没有句子边界,尝试在空格处截断
            last_space = truncated.rfind(' ')
            if last_space > max_length * 0.8:
                return text[:last_space] + "..."
        
        return text[:max_length] + "..."

    def get_root_url(self, url):
        """获取URL的根域名"""
        parsed_url = urlparse(url)
        return f"{parsed_url.scheme}://{parsed_url.netloc}"
