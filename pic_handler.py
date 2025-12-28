from PIL import Image
import aiohttp
import random
import os
import hashlib
import time
import tempfile
import logging
import asyncio
from io import BytesIO


class RssImageHandler:
    """rss处理图片的类"""

    def __init__(self, is_adjust_pic=False):
        """
        初始化图片处理类

        Args:
            is_adjust_pic (bool): 是否防和谐，默认为 False。
        """
        self.is_adjust_pic = is_adjust_pic
        self.logger = logging.getLogger("astrbot")

        # 临时目录
        self.temp_dir = os.path.join(tempfile.gettempdir(), "astrbot_rss_cache")
        if not os.path.exists(self.temp_dir):
            try:
                os.makedirs(self.temp_dir, exist_ok=True)
            except Exception as e:
                self.logger.error(f"[RSS] 创建临时目录失败: {e}")

    def _get_file_path(self, url: str) -> str:
        """根据URL生成唯一的文件路径 (MD5)"""
        try:
            hash_name = hashlib.md5(url.encode("utf-8")).hexdigest()
            url_lower = url.lower()
            if url_lower.endswith(".gif"):
                ext = ".gif"
            elif url_lower.endswith(".mp4"):
                ext = ".mp4"
            elif url_lower.endswith(".png"):
                ext = ".png"
            else:
                ext = ".jpg"
            return os.path.join(self.temp_dir, f"{hash_name}{ext}")
        except Exception:
            return os.path.join(self.temp_dir, f"temp_{int(time.time())}.jpg")

    async def get_image_file(self, image_url: str, max_retries: int = 3) -> str:
        """
        下载图片并保存为本地文件 (包含重试机制)。
        如果文件已存在且有效，则直接返回路径。

        Args:
            image_url (str): 图片链接
            max_retries (int): 最大重试次数，默认3次

        Returns:
            str: 本地文件的绝对路径。如果失败返回 None。
        """
        save_path = self._get_file_path(image_url)

        # 1. 检查缓存：如果文件存在且不为空，直接返回
        if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
            return save_path

        # 2. 下载并处理
        for attempt in range(max_retries):
            try:
                # 设置超时，防止单次请求卡死
                timeout = aiohttp.ClientTimeout(total=30)
                async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
                    async with session.get(image_url) as resp:
                        if resp.status != 200:
                            self.logger.warning(f"[RSS] 第 {attempt + 1}/{max_retries} 次获取图片失败: 状态码 {resp.status} - {image_url}")
                            # 如果不是200，跳过本次循环，进入下一次重试
                            continue

                        # 读取图片数据到内存
                        img_bytes = await resp.read()
                        
                        # 判断是否为 GIF
                        is_gif = image_url.lower().endswith(".gif") or save_path.endswith(".gif")

                        # 3. 防和谐处理逻辑 (如果是GIF则跳过)
                        if self.is_adjust_pic and not is_gif:
                            try:
                                img_data = BytesIO(img_bytes)
                                img = Image.open(img_data)
                                img = img.convert("RGB")

                                width, height = img.size
                                pixels = img.load()
                                corners = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]
                                # 随机选择一个角落修改像素
                                chosen_corner = random.choice(corners)
                                # 修改为接近白色的颜色 (254, 254, 254)
                                pixels[chosen_corner[0], chosen_corner[1]] = (254, 254, 254)

                                # 保存修改后的图片到文件
                                img.save(save_path, format="JPEG", quality=90)
                            except Exception as e:
                                self.logger.error(f"[RSS] 图片防和谐处理失败: {e}，尝试保存原图")
                                with open(save_path, "wb") as f:
                                    f.write(img_bytes)
                        else:
                            # 4. 不需要处理(或GIF)，直接保存原图
                            with open(save_path, "wb") as f:
                                f.write(img_bytes)

                        # 再次确认文件是否写入成功
                        if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                            return save_path
            
            except asyncio.TimeoutError:
                self.logger.warning(f"[RSS] 第 {attempt + 1}/{max_retries} 次下载超时: {image_url}")
            except Exception as e:
                self.logger.warning(f"[RSS] 第 {attempt + 1}/{max_retries} 次下载/保存异常: {e} - {image_url}")
                # 清理可能的损坏文件
                if os.path.exists(save_path):
                    try:
                        os.remove(save_path)
                    except:
                        pass
            
            # 如果不是最后一次尝试，等待一小段时间再重试 (1秒, 2秒...)
            if attempt < max_retries - 1:
                await asyncio.sleep(1 + attempt)

        # 所有重试都失败
        self.logger.error(f"[RSS] 图片最终下载失败，已重试 {max_retries} 次: {image_url}")
        return None

    async def get_video_file(self, video_url: str, max_retries: int = 3) -> str:
        """
        下载视频并保存为本地文件 (包含重试机制)。
        """
        save_path = self._get_file_path(video_url)

        # 1. 检查缓存
        if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
            return save_path

        # 2. 下载
        for attempt in range(max_retries):
            try:
                # 视频文件较大，设置更长的超时
                timeout = aiohttp.ClientTimeout(total=120, connect=10)
                async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
                    async with session.get(video_url) as resp:
                        if resp.status != 200:
                            self.logger.warning(f"[RSS] 视频下载失败: {resp.status} - {video_url}")
                            continue

                        # 流式写入，避免内存占用过大
                        with open(save_path, 'wb') as f:
                            async for chunk in resp.content.iter_chunked(1024):
                                f.write(chunk)

                        if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                            return save_path
            
            except Exception as e:
                self.logger.warning(f"[RSS] 视频下载异常: {e} - {video_url}")
                if os.path.exists(save_path):
                    try:
                        os.remove(save_path)
                    except:
                        pass
            
            if attempt < max_retries - 1:
                await asyncio.sleep(2 + attempt)

        self.logger.error(f"[RSS] 视频最终下载失败: {video_url}")
        return None

    def rotate_image_180(self, file_path: str) -> bool:
        """
        读取指定路径的图片，旋转180度并覆盖保存
        用于在发送失败（风控）时尝试绕过
        """
        try:
            if not os.path.exists(file_path):
                return False
            # GIF 不支持旋转处理
            if file_path.lower().endswith(".gif"):
                return False
            
            # 打开图片
            img = Image.open(file_path)
            # 旋转 180 度
            img = img.transpose(Image.ROTATE_180)
            # 覆盖保存
            img.save(file_path, quality=85)
            
            self.logger.warning(f"[RSS] 以此尝试绕过风控: 已将图片旋转180度 -> {os.path.basename(file_path)}")
            return True
        except Exception as e:
            self.logger.error(f"[RSS] 旋转图片失败: {e}")
            return False

    def cleanup_temp_files(self, max_age_seconds=3600):
        """
        清理过期的临时文件
        Args:
            max_age_seconds: 文件保留的最长时间（秒）
        """
        current_time = time.time()
        count = 0
        try:
            if not os.path.exists(self.temp_dir):
                return

            files = os.listdir(self.temp_dir)
            for filename in files:
                file_path = os.path.join(self.temp_dir, filename)
                if os.path.isfile(file_path):
                    try:
                        file_mtime = os.path.getmtime(file_path)
                        if current_time - file_mtime > max_age_seconds:
                            os.remove(file_path)
                            count += 1
                    except Exception:
                        continue

            if count > 0:
                self.logger.info(f"[RSS] 已清理 {count} 个过期临时文件 (阈值: {max_age_seconds}s)")
        except Exception as e:
            self.logger.error(f"[RSS] 清理临时文件失败: {e}")
