from dataclasses import dataclass, field
from typing import List
import astrbot.api.message_components as Comp

@dataclass
class RSSItem:
    chan_title: str
    title: str
    link: str
    description: str
    pubDate: str
    pubDate_timestamp: int
    pic_urls: List[str]
    
    # 扩展字段
    author: str = ""
    categories: List[str] = field(default_factory=list)
    content: str = ""  # 完整内容(content:encoded)
    summary: str = ""  # 摘要
    enclosure_url: str = ""  # 附件URL(音频/视频等)
    enclosure_type: str = ""  # 附件类型
    comments_url: str = ""  # 评论链接
    guid: str = ""  # 全局唯一标识符

    def __str__(self):
        return f"{self.title} - {self.link} - {self.description} - {self.pubDate}"
    
    def get_display_content(self, max_length: int = 500) -> str:
        """获取用于显示的内容,优先使用完整内容"""
        # 优先使用完整内容,其次是描述,最后是摘要
        content = self.content or self.description or self.summary
        if len(content) > max_length:
            return content[:max_length] + "..."
        return content