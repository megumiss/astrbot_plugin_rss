import aiohttp
import asyncio
import time
import re
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from lxml import etree
from apscheduler.schedulers.asyncio import AsyncIOScheduler
# from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
#     AiocqhttpMessageEvent,
# )
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig
import astrbot.api.message_components as Comp

from aiocqhttp.exceptions import ActionFailed

from .data_handler import DataHandler
from .pic_handler import RssImageHandler
from .rss import RSSItem
from typing import List, Dict, Tuple, Optional


@register(
    "astrbot_plugin_rss",
    "megumiss",
    "RSS订阅插件",
    "1.1.5",
    "https://github.com/megumiss/astrbot_plugin_rss",
)
class RssPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)

        self.logger = logging.getLogger("astrbot")
        self.context = context
        self.config = config
        self.data_handler = DataHandler()
        
        # 提取scheme文件中的配置
        self.title_max_length = config.get("title_max_length")
        self.description_max_length = config.get("description_max_length")
        self.max_items_per_poll = config.get("max_items_per_poll")
        self.t2i = config.get("t2i")
        self.is_hide_url = config.get("is_hide_url")
        self.is_compose = config.get("compose")
        self.is_download_video = config.get("is_download_video", False)
        
        # 图片配置
        self.is_read_pic = config.get("pic_config").get("is_read_pic")
        self.is_adjust_pic = config.get("pic_config").get("is_adjust_pic")
        self.max_pic_item = config.get("pic_config").get("max_pic_item")
        self.cleanup_cron = config.get("pic_config").get("cleanup_cron")
        self.cleanup_retention = config.get("pic_config").get("cleanup_retention")
        # 时区配置
        self.time_zone = config.get("time_zone", "Asia/Shanghai")
        
        self.pic_handler = RssImageHandler(self.is_adjust_pic)
        
        # 缓存与锁
        self.cache_timeout = config.get("cache_timeout", 60) # 缓存有效期
        self.feed_cache: Dict[str, Dict] = {}  # 格式: {url: {'ts': timestamp, 'items': [RSSItem]}}
        self.fetch_locks: Dict[str, asyncio.Lock] = {} # 格式: {url: asyncio.Lock()}

        self.scheduler = AsyncIOScheduler()
        self.scheduler.start()

        # 清理任务
        self._add_cleanup_job()
        self._fresh_asyncIOScheduler()

    def parse_cron_expr(self, cron_expr: str):
        fields = cron_expr.split(" ")
        return {
            "minute": fields[0],
            "hour": fields[1],
            "day": fields[2],
            "month": fields[3],
            "day_of_week": fields[4],
        }

    async def terminate(self):
        """插件卸载/重载时的清理工作"""
        self.logger.info("RSS插件正在卸载，准备停止调度器...")
        try:
            if hasattr(self, 'scheduler') and self.scheduler.running:
                self.scheduler.shutdown()
                self.logger.info("RSS插件调度器已停止。")
        except Exception as e:
            self.logger.error(f"停止RSS插件调度器时发生错误: {e}")

    async def parse_channel_info(self, url):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        try:
            async with aiohttp.ClientSession(trust_env=True,
                                             connector=connector,
                                             timeout=timeout,
                                             headers=headers
                                             ) as session:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        self.logger.error(f"rss: 无法正常打开站点 {url}")
                        return None
                    text = await resp.read()
                    return text
        except asyncio.TimeoutError:
            self.logger.error(f"rss: 请求站点 {url} 超时")
            return None
        except aiohttp.ClientError as e:
            self.logger.error(f"rss: 请求站点 {url} 网络错误: {str(e)}")
            return None
        except Exception as e:
            self.logger.error(f"rss: 请求站点 {url} 发生未知错误: {str(e)}")
            return None

    async def _safe_send_message(self, user: str, message_chain: MessageChain):
        """
        统一发送消息方法，包含风控重试逻辑
        """
        
        # 辅助函数：旋转单个组件（如果它是图片）
        def _try_rotate_component(component) -> bool:
            if isinstance(component, Comp.Image) and component.file:
                file_path = component.file
                if file_path.startswith("file://"):
                    file_path = file_path.replace("file://", "")
                return self.pic_handler.rotate_image_180(file_path)
            return False

        # 辅助函数：处理 Node 内部的内容
        def _process_node_content(node_component) -> bool:
            rotated = False
            node_content = getattr(node_component, "content", [])
            if isinstance(node_content, list):
                for sub_comp in node_content:
                    if _try_rotate_component(sub_comp):
                        rotated = True
            return rotated

        try:
            await self.context.send_message(user, message_chain)
        except ActionFailed as e:
            # 捕获 NTQQ 的 Timeout (风控) 错误
            if e.retcode == 1200:
                self.logger.warning(f"[RSS] 发送失败(Retcode 1200/Timeout)，疑似风控，尝试旋转图片重试...")
                
                has_rotated = False
                # 遍历消息链，找到所有图片组件
                for component in message_chain.chain:
                    # 1. 直接是图片
                    if _try_rotate_component(component):
                        has_rotated = True
                    # 2. 是 Nodes 容器，合并转发
                    elif isinstance(component, Comp.Nodes):
                        # 遍历容器内的所有 Node
                        for sub_node in component.nodes:
                            if _process_node_content(sub_node):
                                has_rotated = True
                    # 3. 是 Node 单个节点，单条转发
                    elif isinstance(component, Comp.Node):
                        if _process_node_content(component):
                            has_rotated = True
                
                if has_rotated:
                    try:
                        # 稍微等待一下再重试
                        await asyncio.sleep(1)
                        self.logger.info("[RSS] 重试发送旋转后的消息...")
                        # 添加简短文字提示
                        message_chain.chain.append(Comp.Plain("\n图片已被旋转"))
                        await self.context.send_message(user, message_chain)
                        return # 重试成功，退出
                    except Exception as retry_e:
                        self.logger.error(f"[RSS] 重试发送依然失败: {retry_e}")
            
            # 如果不是 1200 或者重试也挂了，打印错误但不阻断流程
            self.logger.error(f"[RSS] 消息发送最终失败: {e}")
        except Exception as e:
            self.logger.error(f"[RSS] 发送遇到未知错误: {e}")

    async def cron_task_callback(self, url: str, user: str):
        """定时任务回调"""
        if url not in self.data_handler.data:
            return
        if user not in self.data_handler.data[url]["subscribers"]:
            return

        # 1. 构建日志前缀 [RSS][用户][URL末尾] 以隔离日志
        clean_url = url.split("//")[-1]
        short_url = "..." + clean_url[-25:] if len(clean_url) > 25 else clean_url
        log_prefix = f"[RSS][{user}][{short_url}]"
        self.logger.info(f"{log_prefix} 任务触发")

        sub_info = self.data_handler.data[url]["subscribers"][user]
        last_update = sub_info["last_update"]
        latest_link = sub_info["latest_link"]
        max_items_per_poll = self.max_items_per_poll
        # 拉取 RSS
        rss_items = await self.poll_rss(
            url,
            num=max_items_per_poll,
            after_timestamp=last_update,
            after_link=latest_link,
        )

        self.logger.info(f"{log_prefix} 拉取完成，获取到 {len(rss_items)} 条新内容")
        max_ts = last_update

        # 处理消息发送
        if self.is_compose:
            # 合并转发模式
            node_list = []
            for item in rss_items:
                main_comps, video_comp = await self._get_chain_components(item)
                
                # 1. 文本和图片
                node = Comp.Node(
                    uin=0,
                    name="Astrbot",
                    content=main_comps
                )
                node_list.append(node)
                
                # 2. 视频单独作为一个Node
                if video_comp:
                    video_node = Comp.Node(
                        uin=0,
                        name="Astrbot",
                        content=[video_comp]
                    )
                    node_list.append(video_node)

                if item.pubDate_timestamp > max_ts:
                    max_ts = item.pubDate_timestamp
            
            if len(node_list) > 0:
                # 使用 Comp.Nodes 将列表包装成一个“合并转发容器组件”
                nodes_container = Comp.Nodes(node_list)
                # 构造消息链：必须包含容器组件，且关闭 t2i
                msc = MessageChain(
                    chain=[nodes_container], 
                    use_t2i_=False 
                )
                self.logger.info(f"{log_prefix} 正在发送合并消息 (包含 {len(node_list)} 条)...")
                # 调用统一发送方法
                await self._safe_send_message(user, msc)
        else:
            # 逐条发送模式
            for idx, item in enumerate(rss_items):
                main_comps, video_comp = await self._get_chain_components(item)
                
                # 发送主体内容
                msc = MessageChain(
                    chain=main_comps,
                    use_t2i_=self.t2i
                )
                await self._safe_send_message(user, msc)
                
                # 如果有视频，单独发送一条消息
                if video_comp:
                    video_msc = MessageChain(chain=[video_comp], use_t2i_=False)
                    await self._safe_send_message(user, video_msc)

                self.logger.info(f"{log_prefix} 第 {idx+1}/{len(rss_items)} 条已发送")

                # 只记录 item 的时间戳，不使用系统时间
                if item.pubDate_timestamp > max_ts:
                    max_ts = item.pubDate_timestamp

        # 更新最后更新时间
        if rss_items:
            # 只有当确实处理了消息，才更新数据库
            self.data_handler.data[url]["subscribers"][user]["last_update"] = max_ts
            # 更新最新链接作为双重校验
            self.data_handler.data[url]["subscribers"][user]["latest_link"] = rss_items[0].link
            self.data_handler.save_data()
            self.logger.info(f"RSS 定时任务 {url} 推送成功 - {user}，更新时间至: {max_ts}")
        else:
            self.logger.info(f"RSS 定时任务 {url} 无消息更新 - {user}")

    async def _fetch_and_parse_feed(self, url: str) -> List[RSSItem]:
        """
        [内部方法] 执行实际的网络请求并解析所有 RSS 条目
        不执行任何过滤（时间戳/数量），只负责解析并返回对象列表
        """
        text = await self.parse_channel_info(url)
        if text is None:
            self.logger.error(f"rss: 无法解析站点 {url} 的RSS信息")
            return []
        
        try:
            root = etree.fromstring(text)
        except Exception as e:
            self.logger.error(f"rss: XML解析失败 {url}: {str(e)}")
            return []
        
        # 检测是RSS还是Atom
        is_atom = root.tag.endswith('feed') or 'atom' in root.tag.lower()
        
        # 根据格式选择item路径
        if is_atom:
            items = root.xpath("//*[local-name()='entry']")
        else:
            items = root.xpath("//item")

        rss_items_list = []

        # 获取频道标题，用于填充 RSSItem
        chan_title = (
            self.data_handler.data[url]["info"]["title"]
            if url in self.data_handler.data
            else "未知频道"
        )

        for item in items:
            try:
                # 提取标题
                if is_atom:
                    title_elem = item.xpath("*[local-name()='title']")
                else:
                    title_elem = item.xpath("title")
                
                title = title_elem[0].text if title_elem and title_elem[0].text else "无标题"
                if len(title) > self.title_max_length:
                    title = title[: self.title_max_length] + "..."

                # 提取链接
                if is_atom:
                    link_elem = item.xpath("*[local-name()='link']/@href")
                    link = link_elem[0] if link_elem else ""
                else:
                    link_elem = item.xpath("link")
                    link = link_elem[0].text if link_elem and link_elem[0].text else ""
                
                if link and not re.match(r"^https?://", link):
                    link = self.data_handler.get_root_url(url) + link

                # 提取描述/内容 - 优先使用完整内容
                content = ""
                description = ""
                summary = ""
                
                if is_atom:
                    # Atom格式 - 使用local-name()
                    content_elem = item.xpath("*[local-name()='content']")
                    summary_elem = item.xpath("*[local-name()='summary']")
                    
                    if content_elem and content_elem[0].text:
                        content = content_elem[0].text
                    if summary_elem and summary_elem[0].text:
                        summary = summary_elem[0].text
                    description = content or summary
                else:
                    # RSS格式
                    desc_elem = item.xpath("description")
                    # 尝试获取content:encoded(更完整的内容)
                    content_elem = item.xpath("*[local-name()='encoded']")
                    
                    if content_elem and content_elem[0].text:
                        content = content_elem[0].text
                    if desc_elem and desc_elem[0].text:
                        description = desc_elem[0].text

                # 提取作者
                author = ""
                if is_atom:
                    author_elem = item.xpath("*[local-name()='author']/*[local-name()='name']")
                    if author_elem and author_elem[0].text:
                        author = author_elem[0].text
                else:
                    author_elem = item.xpath("author") or item.xpath("*[local-name()='creator']")
                    if author_elem and author_elem[0].text:
                        author = author_elem[0].text
                
                # 提取分类
                categories = []
                if is_atom:
                    cat_elems = item.xpath("*[local-name()='category']/@term")
                    categories = list(cat_elems)
                else:
                    cat_elems = item.xpath("category")
                    categories = [cat.text for cat in cat_elems if cat.text]
                
                # 提取附件(enclosure)
                enclosure_url = ""
                enclosure_type = ""
                enclosure_elem = item.xpath("enclosure")
                if enclosure_elem:
                    enclosure_url = enclosure_elem[0].get("url", "")
                    enclosure_type = enclosure_elem[0].get("type", "")
                
                # 提取评论链接
                comments_url = ""
                comments_elem = item.xpath("comments")
                if comments_elem and comments_elem[0].text:
                    comments_url = comments_elem[0].text
                
                # 提取GUID
                guid = ""
                if is_atom:
                    guid_elem = item.xpath("*[local-name()='id']")
                    if guid_elem and guid_elem[0].text:
                        guid = guid_elem[0].text
                else:
                    guid_elem = item.xpath("guid")
                    if guid_elem and guid_elem[0].text:
                        guid = guid_elem[0].text
                
                # 处理内容 - 使用完整内容或描述
                full_content = content or description
                
                # 使用 extract_media_urls 来全面提取图片和视频
                media_data = self.data_handler.extract_media_urls(full_content)
                pic_url_list = media_data["images"]
                
                # 如果原生没有附件，但 HTML 中提取到了视频，则将第一个视频作为附件
                if not enclosure_url and media_data["videos"]:
                    enclosure_url = media_data["videos"][0]
                    enclosure_type = "video/mp4" # 假设为 mp4，后续下载会校验
                
                # 清理HTML得到纯文本描述
                clean_description = self.data_handler.strip_html(description or content)
                clean_description = self.data_handler.smart_truncate(clean_description, self.description_max_length)
                
                # 保留完整内容
                clean_content = self.data_handler.strip_html(content) if content else ""

                # 提取日期
                pub_date = ""
                pub_date_timestamp = 0
                
                if is_atom:
                    date_elem = item.xpath("*[local-name()='updated']") or \
                               item.xpath("*[local-name()='published']")
                    if date_elem and date_elem[0].text:
                        pub_date = date_elem[0].text
                else:
                    date_elem = item.xpath("pubDate")
                    if date_elem and date_elem[0].text:
                        pub_date = date_elem[0].text
                
                # 解析日期
                if pub_date:
                    pub_date_timestamp = self._parse_date(pub_date)
                
                # 将解析好的对象加入列表
                rss_items_list.append(
                    RSSItem(
                        chan_title=chan_title,
                        title=title,
                        link=link,
                        description=clean_description,
                        pubDate=pub_date,
                        pubDate_timestamp=pub_date_timestamp,
                        pic_urls=pic_url_list,
                        author=author,
                        categories=categories,
                        content=clean_content,
                        summary=summary,
                        enclosure_url=enclosure_url,
                        enclosure_type=enclosure_type,
                        comments_url=comments_url,
                        guid=guid
                    )
                )

            except Exception as e:
                self.logger.error(f"rss: 解析Rss条目 {url} 失败: {str(e)}")
                continue

        return rss_items_list

    async def _get_feed_data_safe(self, url: str) -> List[RSSItem]:
        """
        获取 Feed 数据，带有 缓存 和 锁 机制
        """
        # 1. 确保每个 URL 都有一个对应的锁，防止并发请求
        if url not in self.fetch_locks:
            self.fetch_locks[url] = asyncio.Lock()
        
        # 2. 上锁
        async with self.fetch_locks[url]:
            current_time = time.time()
            
            # 3. 检查缓存是否有效
            if url in self.feed_cache:
                cache_data = self.feed_cache[url]
                # 如果缓存时间在有效期内（例如60秒），直接返回
                if current_time - cache_data['ts'] < self.cache_timeout:
                    # self.logger.debug(f"[RSS] Hit cache for {url}")
                    return cache_data['items']
            
            # 4. 缓存失效或不存在，执行网络请求
            items = await self._fetch_and_parse_feed(url)
            
            # 5. 更新缓存
            self.feed_cache[url] = {
                'ts': current_time,
                'items': items
            }
            return items

    async def poll_rss(
        self,
        url: str,
        num: int = -1,
        after_timestamp: int = 0,
        after_link: str = "",
    ) -> List[RSSItem]:
        """
        从站点拉取RSS信息 (优化版)
        先从缓存/网络获取全量数据，再根据 timestamp 进行过滤
        """
        # 获取全量条目（带缓存）
        all_items = await self._get_feed_data_safe(url)
        
        filtered_items = []
        cnt = 0
        
        # 遍历全量条目进行过滤
        for item in all_items:
            is_new = False
            
            # 判断是否为新内容
            if item.pubDate_timestamp > 0:
                # 时间戳必须严格大于上次更新时间
                is_new = item.pubDate_timestamp > after_timestamp
            else:
                # 无时间戳退化为链接判断
                is_new = item.link != after_link
            
            if is_new:
                filtered_items.append(item)
                cnt += 1
                if num != -1 and cnt >= num:
                    break
            elif item.pubDate_timestamp > 0:
                # 假设 RSS 是按时间倒序排列的，一旦遇到旧消息，后面的肯定更旧
                # 注意：如果 RSS 乱序，这里可能需要调整，但标准 RSS 默认是有序的
                if item.pubDate_timestamp <= after_timestamp:
                    break
        
        return filtered_items

    def _parse_date(self, date_str: str) -> int:
        """解析各种日期格式为时间戳"""
        if not date_str:
            return 0
        
        # 常见日期格式
        date_formats = [
            "%a, %d %b %Y %H:%M:%S %z",      # RSS标准: Wed, 02 Oct 2002 13:00:00 GMT
            "%a, %d %b %Y %H:%M:%S GMT",     # RSS GMT格式
            "%Y-%m-%dT%H:%M:%S%z",           # ISO 8601: 2002-10-02T13:00:00+00:00
            "%Y-%m-%dT%H:%M:%SZ",            # ISO 8601 UTC: 2002-10-02T13:00:00Z
            "%Y-%m-%dT%H:%M:%S.%f%z",        # ISO 8601带毫秒
            "%Y-%m-%dT%H:%M:%S.%fZ",         # ISO 8601 UTC带毫秒
            "%Y-%m-%d %H:%M:%S",             # 简单格式
            "%Y/%m/%d %H:%M:%S",             # 斜杠分隔
        ]
        
        # 预处理
        date_str = date_str.strip()
        if "GMT" in date_str:
            date_str = date_str.replace("GMT", "+0000")
            
        current_ts = int(time.time())
        for fmt in date_formats:
            try:
                dt = datetime.strptime(date_str, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = int(dt.timestamp())
                
                # 未来时间防护：超过1小时的未来时间视为异常，修正为当前时间
                if ts > current_ts + 3600:
                     return current_ts
                return ts
            except ValueError:
                continue
        
        return current_ts
    
    def parse_rss_url(self, url: str) -> str:
        """解析RSS URL，确保以http或https开头"""
        if not re.match(r"^https?://", url):
            if not url.startswith("/"):
                url = "/" + url
            url = "https://" + url
        return url

    def _fresh_asyncIOScheduler(self):
        """刷新定时任务"""
        self.logger.info("刷新定时任务")
        
        # 1. 初始化白名单，默认包含系统级清理任务ID
        active_job_ids = {"rss_image_cleanup"}
        
        # 2. 收集所有活跃的订阅任务ID
        for url, info in self.data_handler.data.items():
            if url in ["rsshub_endpoints", "settings"]:
                continue
            
            for user, sub_info in info["subscribers"].items():
                # 构造唯一 ID：URL + User
                job_id = f"{url}|{user}"
                active_job_ids.add(job_id)
                
                try:
                    # 添加或更新任务
                    # id: 指定固定ID
                    # replace_existing: 如果任务已存在，则更新触发参数
                    self.scheduler.add_job(
                        self.cron_task_callback,
                        "cron",
                        **self.parse_cron_expr(sub_info["cron_expr"]),
                        args=[url, user],
                        id=job_id,
                        replace_existing=True
                    )
                except Exception as e:
                    self.logger.error(f"添加定时任务失败 {job_id}: {str(e)}")

        # 3. 清理已经不再配置中的废弃任务
        # 获取调度器中当前所有的任务
        current_jobs = self.scheduler.get_jobs()
        for job in current_jobs:
            # 如果调度器里的任务ID不在我们需要活跃的列表中，说明该订阅已被删除
            if job.id not in active_job_ids:
                try:
                    self.scheduler.remove_job(job.id)
                    self.logger.info(f"清理废弃任务: {job.id}")
                except Exception as e:
                    self.logger.error(f"清理废弃任务失败 {job.id}: {str(e)}")

        self.logger.info(f"定时任务刷新完成，当前运行任务数: {len(self.scheduler.get_jobs())}")

    def _add_cleanup_job(self):
        """添加清理临时文件的定时任务"""
        try:
            # 解析 Cron 表达式
            cron_args = self.parse_cron_expr(self.cleanup_cron)
            retention_seconds = self.cleanup_retention * 60
            self.logger.info(f"[RSS] 注册图片/视频清理任务: Cron[{self.cleanup_cron}] 保留时长[{self.cleanup_retention}分钟]")
            
            self.scheduler.add_job(
                self.pic_handler.cleanup_temp_files,
                "cron",
                **cron_args,
                args=[retention_seconds],
                id="rss_image_cleanup",
                replace_existing=True
            )
        except Exception as e:
            self.logger.error(f"[RSS] 注册图片清理任务失败: {e}")
            retention_seconds = self.cleanup_retention * 60
            self.scheduler.add_job(
                self.pic_handler.cleanup_temp_files,
                "interval",
                minutes=30,
                args=[retention_seconds],
                id="rss_image_cleanup",
                replace_existing=True
            )

    async def _add_url(self, url: str, cron_expr: str, message: AstrMessageEvent):
        """内部方法:添加URL订阅的共用逻辑"""
        user = message.unified_msg_origin
        if url in self.data_handler.data:
            latest_item = await self.poll_rss(url)
            if not latest_item:
                return message.plain_result(f"无法获取RSS内容,请检查URL是否正确")
            self.data_handler.data[url]["subscribers"][user] = {
                "cron_expr": cron_expr,
                "last_update": latest_item[0].pubDate_timestamp,
                "latest_link": latest_item[0].link,
            }
        else:
            try:
                text = await self.parse_channel_info(url)
                if text is None:
                    return message.plain_result(f"无法访问该RSS源,请检查URL是否正确")
                title, desc = self.data_handler.parse_channel_text_info(text)
                latest_item = await self.poll_rss(url)
                if not latest_item:
                    return message.plain_result(f"RSS源无可用内容,请检查URL是否正确")
            except Exception as e:
                return message.plain_result(f"解析频道信息失败: {str(e)}")

            self.data_handler.data[url] = {
                "subscribers": {
                    user: {
                        "cron_expr": cron_expr,
                        "last_update": latest_item[0].pubDate_timestamp,
                        "latest_link": latest_item[0].link,
                    }
                },
                "info": {
                    "title": title,
                    "description": desc,
                },
            }
        self.data_handler.save_data()
        return self.data_handler.data[url]["info"]

    async def _get_chain_components(self, item: RSSItem) -> Tuple[List[any], Optional[any]]:
        """
        组装消息链
        Returns:
            Tuple[List[Component], Optional[Component]]: (主体消息组件列表, 视频组件(若有))
        """
        comps = []
        # 收集所有的文本行
        text_lines = []
        video_comp = None
        
        # 标题和频道信息
        text_lines.append(f"📰 {item.chan_title}")
        text_lines.append("─" * 30)
        text_lines.append(f"📌 {item.title}")
        
        # 添加作者和分类
        meta_info = []
        if item.author:
            meta_info.append(f"👤 {item.author}")
        if item.categories:
              # 最多显示3个分类
            meta_info.append(f"🏷️ {', '.join(item.categories[:3])}")
        if item.pubDate and item.pubDate_timestamp > 0:
            # 格式化日期显示
            try:
                target_tz = ZoneInfo(self.time_zone)
                dt = datetime.fromtimestamp(item.pubDate_timestamp, target_tz)
                meta_info.append(f"🕒 {dt.strftime('%Y-%m-%d %H:%M')}")
            except Exception as e:
                self.logger.warning(f"[RSS] 时间格式化失败: {e}")
                dt = datetime.fromtimestamp(item.pubDate_timestamp)
                meta_info.append(f"🕒 {dt.strftime('%Y-%m-%d %H:%M')}")
        
        if meta_info:
            text_lines.append(" | ".join(meta_info))
        
        text_lines.append("─" * 30)
        
        # 内容 - 使用完整内容或描述
        content_text = item.get_display_content(self.description_max_length)
        if content_text:
            # 确保内容本身前后不带多余空行
            text_lines.append(content_text.strip())
        
        # 链接
        if not self.is_hide_url and item.link:
            # 添加一个空行做分隔
            text_lines.append("") 
            text_lines.append(f"🔗 {item.link}")
        
        # 附件信息(音频/视频)
        if item.enclosure_url:
            text_lines.append("") # 空行分隔
            enclosure_info = "📎 附件: "
            is_video = False
            
            if "audio" in item.enclosure_type:
                enclosure_info += "🎵 音频 - "
            elif "video" in item.enclosure_type:
                enclosure_info += "🎬 视频 - "
                is_video = True
            else:
                enclosure_info += "📄 文件 - "
            enclosure_info += item.enclosure_url
            text_lines.append(enclosure_info)

            # 视频组件处理
            if is_video:
                if self.is_download_video:
                    file_path = await self.pic_handler.get_video_file(item.enclosure_url)
                    if file_path:
                        video_comp = Comp.Video.fromFileSystem(path=file_path)
                    else:
                        text_lines.append("[❌] 视频下载失败")
                else:
                    video_comp = Comp.Video.fromURL(url=item.enclosure_url)
            
        # 评论链接
        if item.comments_url:
            text_lines.append(f"💬 评论: {item.comments_url}")

        # 图片标题
        has_images = self.is_read_pic and item.pic_urls
        if has_images:
            text_lines.append("") # 空行分隔
            text_lines.append(f"📷 图片 ({len(item.pic_urls)}张):")

        # 生成文本
        final_text = "\n".join(text_lines)
        comps.append(Comp.Plain(final_text))

        # 处理图片组件
        if has_images:
            # 如果max_pic_item为-1则不限制图片数量
            temp_max_pic_item = len(item.pic_urls) if self.max_pic_item == -1 else self.max_pic_item
            
            for idx, pic_url in enumerate(item.pic_urls[:temp_max_pic_item], 1):
                # 获取本地路径
                file_path = await self.pic_handler.get_image_file(pic_url)
                
                if file_path:
                    # 使用 fromFileSystem 发送本地文件
                    comps.append(Comp.Image.fromFileSystem(file_path))
                else:
                    # 图片加载失败的信息
                    comps.append(Comp.Plain(f"\n[❌] 图{idx} 加载失败\n"))
            
            # 如果还有更多图片未显示
            if len(item.pic_urls) > temp_max_pic_item:
                count = len(item.pic_urls) - temp_max_pic_item
                comps.append(Comp.Plain(f"\n... 还有 {count} 张图片未显示"))
        
        return comps, video_comp


    def _is_url_or_ip(self,text: str) -> bool:
        """
        判断一个字符串是否为网址（http/https 开头）或 IP 地址。
        """
        url_pattern = r"^(?:http|https)://.+$"
        ip_pattern = r"^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$"
        return bool(re.match(url_pattern, text) or re.match(ip_pattern, text))

    @filter.command_group("rss", alias={"RSS"})
    def rss(self):
        """RSS订阅插件

        可以订阅和管理多个RSS源，支持cron表达式设置更新频率

        cron 表达式格式：
        * * * * *，分别表示分钟 小时 日 月 星期，* 表示任意值，支持范围和逗号分隔。例：
        1. 0 0 * * * 表示每天 0 点触发。
        2. 0/5 * * * * 表示每 5 分钟触发。
        3. 0 9-18 * * * 表示每天 9 点到 18 点触发。
        4. 0 0 1,15 * * 表示每月 1 号和 15 号 0 点触发。
        星期的取值范围是 0-6，0 表示星期天。
        """
        pass

    @rss.group("rsshub")
    def rsshub(self, event: AstrMessageEvent):
        """RSSHub相关操作

        可以添加、查看、删除RSSHub的端点
        """
        pass

    @rsshub.command("add")
    async def rsshub_add(self, event: AstrMessageEvent, url: str):
        """添加一个RSSHub端点

        Args:
            url: RSSHub服务器地址，例如：https://rsshub.app
        """
        if url.endswith("/"):
            url = url[:-1]
        # 检查是否为url或ip
        if not self._is_url_or_ip(url):
            yield event.plain_result("请输入正确的URL")
            return
        # 检查该网址是否已存在
        elif url in self.data_handler.data["rsshub_endpoints"]:
            yield event.plain_result("该RSSHub端点已存在")
            return
        else:
            self.data_handler.data["rsshub_endpoints"].append(url)
            self.data_handler.save_data()
            yield event.plain_result("添加成功")

    @rsshub.command("list")
    async def rsshub_list(self, event: AstrMessageEvent):
        """列出所有已添加的RSSHub端点"""
        ret = "当前Bot添加的rsshub endpoint：\n"
        yield event.plain_result(
            ret
            + "\n".join(
                [
                    f"{i}: {x}"
                    for i, x in enumerate(self.data_handler.data["rsshub_endpoints"])
                ]
            )
        )

    @rsshub.command("remove")
    async def rsshub_remove(self, event: AstrMessageEvent, idx: int):
        """删除一个RSSHub端点

        Args:
            idx: 要删除的端点索引，可通过list命令查看
        """
        if idx < 0 or idx >= len(self.data_handler.data["rsshub_endpoints"]):
            yield event.plain_result("索引越界")
            return
        else:
            # TODO:删除对应的定时任务
            self.scheduler.remove_job()
            self.data_handler.data["rsshub_endpoints"].pop(idx)
            self.data_handler.save_data()
            yield event.plain_result("删除成功")

    @rss.command("add")
    async def add_command(
        self,
        event: AstrMessageEvent,
        idx: int,
        route: str,
        minute: str,
        hour: str,
        day: str,
        month: str,
        day_of_week: str,
    ):
        """通过RSSHub路由添加订阅

        Args:
            idx: RSSHub端点索引，可通过/rss rsshub list查看
            route: RSSHub路由，需以/开头
            minute: Cron表达式分钟字段
            hour: Cron表达式小时字段
            day: Cron表达式日期字段
            month: Cron表达式月份字段
            day_of_week: Cron表达式星期字段
        """
        if idx < 0 or idx >= len(self.data_handler.data["rsshub_endpoints"]):
            yield event.plain_result(
                "索引越界, 请使用 /rss rsshub list 查看已经添加的 rsshub endpoint"
            )
            return
        if not route.startswith("/"):
            yield event.plain_result("路由必须以 / 开头")
            return

        url = self.data_handler.data["rsshub_endpoints"][idx] + route
        cron_expr = f"{minute} {hour} {day} {month} {day_of_week}"

        ret = await self._add_url(url, cron_expr, event)
        if isinstance(ret, MessageEventResult):
            yield ret
            return
        else:
            chan_title = ret["title"]
            chan_desc = ret["description"]

        # 刷新定时任务
        self._fresh_asyncIOScheduler()

        yield event.plain_result(
            f"添加成功。频道信息：\n标题: {chan_title}\n描述: {chan_desc}"
        )

    @rss.command("add-url")
    async def add_url_command(
        self,
        event: AstrMessageEvent,
        url: str,
        minute: str,
        hour: str,
        day: str,
        month: str,
        day_of_week: str,
    ):
        """直接通过Feed URL添加订阅

        Args:
            url: RSS Feed的完整URL
            minute: Cron表达式分钟字段
            hour: Cron表达式小时字段
            day: Cron表达式日期字段
            month: Cron表达式月份字段
            day_of_week: Cron表达式星期字段
        """
        cron_expr = f"{minute} {hour} {day} {month} {day_of_week}"
        ret = await self._add_url(url, cron_expr, event)
        if isinstance(ret, MessageEventResult):
            yield ret
            return
        else:
            chan_title = ret["title"]
            chan_desc = ret["description"]

        # 刷新定时任务
        self._fresh_asyncIOScheduler()

        yield event.plain_result(
            f"添加成功。频道信息：\n标题: {chan_title}\n描述: {chan_desc}"
        )

    @rss.command("list")
    async def list_command(self, event: AstrMessageEvent):
        """列出当前所有订阅的RSS频道"""
        user = event.unified_msg_origin
        ret = "当前订阅的频道：\n"
        subs_urls = self.data_handler.get_subs_channel_url(user)
        cnt = 0
        for url in subs_urls:
            info = self.data_handler.data[url]["info"]
            ret += f"{cnt}. {info['title']} - {info['description']}\n"
            cnt += 1
        yield event.plain_result(ret)

    @rss.command("remove")
    async def remove_command(self, event: AstrMessageEvent, idx: int):
        """删除一个RSS订阅

        Args:
            idx: 要删除的订阅索引，可通过/rss list查看
        """
        subs_urls = self.data_handler.get_subs_channel_url(event.unified_msg_origin)
        if idx < 0 or idx >= len(subs_urls):
            yield event.plain_result("索引越界, 请使用 /rss list 查看已经添加的订阅")
            return
        url = subs_urls[idx]
        self.data_handler.data[url]["subscribers"].pop(event.unified_msg_origin)

        self.data_handler.save_data()

        # 刷新定时任务
        self._fresh_asyncIOScheduler()
        yield event.plain_result("删除成功")

    @rss.command("get")
    async def get_command(self, event: AstrMessageEvent, idx: int):
        """获取指定订阅的最新内容

        Args:
            idx: 要查看的订阅索引，可通过/rss list查看
        """
        subs_urls = self.data_handler.get_subs_channel_url(event.unified_msg_origin)
        if idx < 0 or idx >= len(subs_urls):
            yield event.plain_result("索引越界, 请使用 /rss list 查看已经添加的订阅")
            return
        url = subs_urls[idx]
        rss_items = await self.poll_rss(url)
        if not rss_items:
            yield event.plain_result("没有新的订阅内容")
            return
        item = rss_items[0]
        # 分解MessageSesion
        platform_name, message_type, session_id = event.unified_msg_origin.split(":")
        
        # 构造返回消息链
        main_comps, video_comp = await self._get_chain_components(item)
        
        # 区分平台构造消息链
        if self.is_compose:
            # 1. 文本和图片 Node
            node = Comp.Node(
                    uin=0,
                    name="Astrbot",
                    content=main_comps
                )
            nodes_list = [node]

            # 2. 视频 Node
            if video_comp:
                video_node = Comp.Node(
                    uin=0,
                    name="Astrbot",
                    content=[video_comp]
                )
                nodes_list.append(video_node)
            
            # 发送合并消息
            nodes_container = Comp.Nodes(nodes_list)
            target_message_chain = MessageChain(chain=[nodes_container], use_t2i_=False)
            await self._safe_send_message(event.unified_msg_origin, target_message_chain)
        else:
            # 单条发送模式
            target_message_chain = MessageChain(chain=main_comps, use_t2i_=self.t2i)
            await self._safe_send_message(event.unified_msg_origin, target_message_chain)
            
            # 视频独立发送
            if video_comp:
                video_chain = MessageChain(chain=[video_comp], use_t2i_=False)
                await self._safe_send_message(event.unified_msg_origin, video_chain)