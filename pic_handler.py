from PIL import Image
import aiohttp
import random
import os
import hashlib
import time
import tempfile
import logging
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

        # 设置临时目录
        # 会在系统临时目录下创建一个 astrbot_rss_cache 文件夹
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
            return os.path.join(self.temp_dir, f"{hash_name}.jpg")
        except Exception:
            # 以此作为fallback，虽然基本不会发生
            return os.path.join(self.temp_dir, f"temp_{int(time.time())}.jpg")

    async def get_image_file(self, image_url: str) -> str:
        """
        下载图片并保存为本地文件。
        如果文件已存在且有效，则直接返回路径。

        Returns:
            str: 本地文件的绝对路径。如果失败返回 None。
        """
        save_path = self._get_file_path(image_url)

        # 1. 检查缓存：如果文件存在且不为空，直接返回
        if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
            return save_path

        # 2. 下载并处理
        try:
            # 回退到原始的请求方式
            async with aiohttp.ClientSession(trust_env=True) as session:
                async with session.get(image_url) as resp:
                    if resp.status != 200:
                        self.logger.warning(
                            f"[RSS] 无法从URL获取图片: 状态码 {resp.status} - {image_url}"
                        )
                        return None

                    # 读取图片数据到内存
                    img_bytes = await resp.read()
                    img_data = BytesIO(img_bytes)

                    # 3. 防和谐处理逻辑
                    if self.is_adjust_pic:
                        try:
                            img = Image.open(img_data)
                            img = img.convert("RGB")  # 确保可以保存为 jpg

                            width, height = img.size
                            pixels = img.load()

                            # 随机选择四个角落之一
                            corners = [
                                (0, 0),  # 左上角
                                (width - 1, 0),  # 右上角
                                (0, height - 1),  # 左下角
                                (width - 1, height - 1),  # 右下角
                            ]

                            # 随机选择一个角落修改像素
                            chosen_corner = random.choice(corners)
                            # 修改为接近白色的颜色 (254, 254, 254)
                            pixels[chosen_corner[0], chosen_corner[1]] = (254, 254, 254)

                            # 保存修改后的图片到文件
                            img.save(save_path, format="JPEG", quality=90)
                        except Exception as e:
                            self.logger.error(
                                f"[RSS] 图片防和谐处理失败: {e}，尝试保存原图"
                            )
                            # 处理失败则回退保存原图
                            with open(save_path, "wb") as f:
                                f.write(img_bytes)
                    else:
                        # 4. 不需要处理，直接保存原图
                        with open(save_path, "wb") as f:
                            f.write(img_bytes)

                    # 再次确认文件是否写入成功
                    if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                        return save_path
                    else:
                        return None

        except aiohttp.ClientError as e:
            self.logger.error(f"[RSS] 网络错误无法获取图片 '{image_url}': {e}")
            return None
        except Exception as e:
            self.logger.error(f"[RSS] 图片下载/保存未知错误: {e}")
            # 如果生成了坏文件，清理掉
            if os.path.exists(save_path):
                try:
                    os.remove(save_path)
                except:
                    pass
            return None

    def cleanup_temp_files(self, max_age_seconds=3600):
        """
        清理过期的临时文件
        Args:
            max_age_seconds: 文件保留的最长时间（秒），默认1小时
        """
        current_time = time.time()
        count = 0
        try:
            if not os.path.exists(self.temp_dir):
                return

            for filename in os.listdir(self.temp_dir):
                file_path = os.path.join(self.temp_dir, filename)
                # 检查是否是文件
                if os.path.isfile(file_path):
                    try:
                        # 获取文件修改时间
                        file_mtime = os.path.getmtime(file_path)
                        if current_time - file_mtime > max_age_seconds:
                            os.remove(file_path)
                            count += 1
                    except Exception:
                        continue

            if count > 0:
                self.logger.info(f"[RSS] 已清理 {count} 个过期临时图片文件")
        except Exception as e:
            self.logger.error(f"[RSS] 清理临时文件失败: {e}")
