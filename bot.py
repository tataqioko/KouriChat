import logging
import random
from datetime import datetime
import threading
import time
import os
from database import Session, ChatMessage
from config import (
    DEEPSEEK_API_KEY, MAX_TOKEN, ROBOT_WX_NAME, TEMPERATURE, MODEL, DEEPSEEK_BASE_URL, LISTEN_LIST,
    IMAGE_MODEL, TEMP_IMAGE_DIR, MAX_GROUPS, PROMPT_NAME, EMOJI_DIR, TTS_API_URL, VOICE_DIR
)
from wxauto import WeChat
from openai import OpenAI
import requests
from typing import Optional
import re
import sys


# 获取微信窗口对象
wx = WeChat()

# 设置监听列表
listen_list = LISTEN_LIST

# 循环添加监听对象，移除savepic=True参数
for i in listen_list:
    wx.AddListenChat(who=i)  # 移除 savepic=True

# 修改等待时间为更短的间隔
wait = 0.5  # 从1秒改为0.5秒

# 初始化OpenAI客户端（替换原有requests方式）
client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url=DEEPSEEK_BASE_URL,
    default_headers={"Content-Type": "application/json"}  # 添加默认请求头
)

# 获取程序根目录
root_dir = os.path.dirname(os.path.abspath(__file__))
file_path = os.path.join(root_dir, "prompts", PROMPT_NAME)

# 新增全局变量
user_queues = {}  # 用户消息队列管理
queue_lock = threading.Lock()  # 队列访问锁
chat_contexts = {}  # 存储上下文

# 读取文件内容到变量
with open(file_path, "r", encoding="utf-8") as file:
    prompt_content = file.read()


# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 添加图像生成相关常量
IMAGE_API_URL = f"{DEEPSEEK_BASE_URL}/images/generations"  # 需要在config.py中添加基础URL

# 添加临时目录初始化
temp_dir = os.path.join(root_dir, TEMP_IMAGE_DIR)
if not os.path.exists(temp_dir):
    os.makedirs(temp_dir)

def save_message(sender_id, sender_name, message, reply):
    # 保存聊天记录到数据库
    try:
        session = Session()
        chat_message = ChatMessage(
            sender_id=sender_id,
            sender_name=sender_name,
            message=message,
            reply=reply
        )
        session.add(chat_message)
        session.commit()
        session.close()
    except Exception as e:
        print(f"保存消息失败: {str(e)}")


def generate_image(prompt: str) -> Optional[str]:
    """
    调用API生成图像
    """
    try:
        headers = {
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json"
        }
        
        data = {
            "model": IMAGE_MODEL,
            "prompt": prompt
        }
        
        response = requests.post(
            f"{DEEPSEEK_BASE_URL}/images/generations",
            headers=headers,
            json=data
        )
        response.raise_for_status()
        
        result = response.json()
        if "data" in result and len(result["data"]) > 0:
            return result["data"][0]["url"]
        return None
        
    except Exception as e:
        logger.error(f"图像生成失败: {str(e)}")
        return None

def is_image_generation_request(text: str) -> bool:
    """
    判断是否为图像生成请求
    """
    # 基础动词
    draw_verbs = ["画", "绘", "生成", "创建", "做"]
    
    # 图像相关词
    image_nouns = ["图", "图片", "画", "照片", "插画", "像"]
    
    # 数量词
    quantity = ["一下", "一个", "一张", "个", "张", "幅"]
    
    # 组合模式
    patterns = [
        # 直接画xxx模式
        r"画.*[猫狗人物花草山水]",
        r"画.*[一个张只条串份副幅]",
        # 帮我画xxx模式
        r"帮.*画.*",
        r"给.*画.*",
        # 生成xxx图片模式
        r"生成.*图",
        r"创建.*图",
        # 能不能画xxx模式
        r"能.*画.*吗",
        r"可以.*画.*吗",
        # 想要xxx图模式
        r"要.*[张个幅].*图",
        r"想要.*图",
        # 其他常见模式
        r"做[一个张]*.*图",
        r"画画",
        r"画一画",
    ]
    
    # 1. 检查正则表达式模式
    if any(re.search(pattern, text) for pattern in patterns):
        return True
        
    # 2. 检查动词+名词组合
    for verb in draw_verbs:
        for noun in image_nouns:
            if f"{verb}{noun}" in text:
                return True
            # 检查带数量词的组合
            for q in quantity:
                if f"{verb}{q}{noun}" in text:
                    return True
                if f"{verb}{noun}{q}" in text:
                    return True
    
    # 3. 检查特定短语
    special_phrases = [
        "帮我画", "给我画", "帮画", "给画",
        "能画吗", "可以画吗", "会画吗",
        "想要图", "要图", "需要图",
    ]
    
    if any(phrase in text for phrase in special_phrases):
        return True
    
    return False

def is_emoji_request(text: str) -> bool:
    """
    判断是否为表情包请求
    """
    # 直接请求表情包的关键词
    emoji_keywords = ["表情包", "表情", "斗图", "gif", "动图"]
    
    # 情感表达关键词
    emotion_keywords = ["开心", "难过", "生气", "委屈", "高兴", "伤心",
                       "哭", "笑", "怒", "喜", "悲", "乐", "泪", "哈哈",
                       "呜呜", "嘿嘿", "嘻嘻", "哼", "啊啊", "呵呵","可爱"]
    
    # 检查直接请求
    if any(keyword in text.lower() for keyword in emoji_keywords):
        return True
        
    # 检查情感表达
    if any(keyword in text for keyword in emotion_keywords):
        return True
        
    return False

def get_random_emoji() -> Optional[str]:
    """
    从表情包目录随机获取一个表情包
    """
    try:
        emoji_dir = os.path.join(root_dir, EMOJI_DIR)
        if not os.path.exists(emoji_dir):
            logger.error(f"表情包目录不存在: {emoji_dir}")
            return None
            
        emoji_files = [f for f in os.listdir(emoji_dir) 
                      if f.lower().endswith(('.gif', '.jpg', '.png', '.jpeg'))]
        
        if not emoji_files:
            return None
            
        random_emoji = random.choice(emoji_files)
        return os.path.join(emoji_dir, random_emoji)
    except Exception as e:
        logger.error(f"获取表情包失败: {str(e)}")
        return None

def get_deepseek_response(message, user_id):
    try:
        # 检查是否为图像生成请求
        if is_image_generation_request(message):
            image_url = generate_image(message)
            if image_url:
                # 下载图片并保存到临时文件
                img_response = requests.get(image_url)
                if img_response.status_code == 200:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    temp_path = os.path.join(temp_dir, f"image_{timestamp}.jpg")
                    with open(temp_path, "wb") as f:
                        f.write(img_response.content)
                    return f"[IMAGE]{temp_path}[/IMAGE]\n这是按照主人您的要求生成的图片\\(^o^)/~"
                else:
                    return "抱歉主人，图片生成成功但下载失败，请稍后重试。"
            else:
                return "抱歉主人，图片生成失败，请稍后重试。"

        # 文本处理逻辑
        print(f"调用 DeepSeek API - 用户ID: {user_id}, 消息: {message}")
        with queue_lock:
            if user_id not in chat_contexts:
                chat_contexts[user_id] = []

            chat_contexts[user_id].append({"role": "user", "content": message})

            while len(chat_contexts[user_id]) > MAX_GROUPS * 2:
                if len(chat_contexts[user_id]) >= 2:
                    del chat_contexts[user_id][0]
                    del chat_contexts[user_id][0]
                else:
                    del chat_contexts[user_id][0]

        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": prompt_content},
                    *chat_contexts[user_id][-MAX_GROUPS * 2:]
                ],
                temperature=TEMPERATURE,
                max_tokens=MAX_TOKEN,
                stream=False
            )
        except Exception as api_error:
            logger.error(f"API调用失败: {str(api_error)}")
            return "抱歉主人，我现在有点累，请稍后再试..."

        if not response.choices:
            logger.error("API返回空choices: %s", response)
            return "抱歉主人，服务响应异常，请稍后再试"

        reply = response.choices[0].message.content
        print(f"API响应 - 用户ID: {user_id}")
        print(f"响应内容: {reply}")

        with queue_lock:
            chat_contexts[user_id].append({"role": "assistant", "content": reply})
            
        return reply

    except Exception as e:
        logger.error(f"DeepSeek调用失败: {str(e)}", exc_info=True)
        return "抱歉主人，刚刚不小心睡着了..."

def is_voice_request(text: str) -> bool:
    """
    判断是否为语音请求
    """
    voice_keywords = ["语音", "说话", "念", "读", "朗读", "播放", "声音"]
    return any(keyword in text for keyword in voice_keywords)

def generate_voice(text: str) -> Optional[str]:
    """
    调用TTS API生成语音
    """
    try:
        # 确保语音目录存在
        voice_dir = os.path.join(root_dir, VOICE_DIR)
        if not os.path.exists(voice_dir):
            os.makedirs(voice_dir)
            
        # 生成唯一的文件名
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        voice_path = os.path.join(voice_dir, f"voice_{timestamp}.wav")
        
        # 调用TTS API
        response = requests.get(f"{TTS_API_URL}?text={text}", stream=True)
        if response.status_code == 200:
            with open(voice_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return voice_path
        else:
            logger.error(f"语音生成失败: {response.status_code}")
            return None
            
    except Exception as e:
        logger.error(f"语音生成失败: {str(e)}")
        return None

def process_user_messages(user_id):
    with queue_lock:
        if user_id not in user_queues:
            return
        user_data = user_queues.pop(user_id)
        messages = user_data['messages']
        sender_name = user_data['sender_name']
        username = user_data['username']
        recipient_id = user_data.get('chat_id', user_id)

    # 优化消息合并逻辑，只保留最后5条消息
    messages = messages[-5:]  # 限制处理的消息数量
    merged_message = ' \\ '.join(messages)
    print(f"处理合并消息 ({sender_name}): {merged_message}")

    # 获取API回复
    reply = get_deepseek_response(merged_message, user_id)
    if "</think>" in reply:
         reply = reply.split("</think>", 1)[1].strip()
    # 修改发送逻辑部分
    try:
        # 首先检查是否为语音请求
        if is_voice_request(merged_message):
            # 获取API回复
            reply = get_deepseek_response(merged_message, user_id)
            if "</think>" in reply:
                reply = reply.split("</think>", 1)[1].strip()
            
            # 生成语音
            voice_path = generate_voice(reply)
            if voice_path:
                try:
                    # 发送语音文件
                    wx.SendFiles(filepath=voice_path, who=user_id)
                except Exception as e:
                    logger.error(f"发送语音失败: {str(e)}")
                    # 如果语音发送失败，退回到文本发送
                    wx.SendMsg(msg=reply, who=user_id)
                finally:
                    # 删除临时语音文件
                    try:
                        os.remove(voice_path)
                    except Exception as e:
                        logger.error(f"删除临时语音文件失败: {str(e)}")
            else:
                # 语音生成失败，发送文本
                wx.SendMsg(msg=reply, who=user_id)
            return

        # 首先检查是否需要发送表情包
        if is_emoji_request(merged_message):
            emoji_path = get_random_emoji()
            if emoji_path:
                try:
                    # 先发送表情包
                    wx.SendFiles(filepath=emoji_path, who=user_id)
                except Exception as e:
                    logger.error(f"发送表情包失败: {str(e)}")

        # 获取API回复
        reply = get_deepseek_response(merged_message, user_id)
        if "</think>" in reply:
         reply = reply.split("</think>", 1)[1].strip()

        # 处理回复
        if '[IMAGE]' in reply:
            # 处理图片回复
            img_path = reply.split('[IMAGE]')[1].split('[/IMAGE]')[0].strip()
            if os.path.exists(img_path):
                try:
                    wx.SendFiles(filepath=img_path, who=user_id)
                    text_msg = reply.split('[/IMAGE]')[1].strip()
                    if text_msg:
                        wx.SendMsg(msg=text_msg, who=user_id)
                finally:
                    try:
                        os.remove(img_path)
                    except Exception as e:
                        logger.error(f"删除临时图片失败: {str(e)}")
        elif '\\' in reply:
            parts = [p.strip() for p in reply.split('\\') if p.strip()]
            for idx,part in enumerate(parts):
                # 仅在分段回复的第一句加上@sender_name（群聊回复时才加）
                if idx == 0 and recipient_id != user_id:
                    part = f"@{sender_name} {part}"
                wx.SendMsg(msg=part, who=user_id)
                time.sleep(random.randint(3,5))
        else:
            wx.SendMsg(msg=reply, who=user_id)
            
    except Exception as e:
        logger.error(f"发送回复失败: {str(e)}")

    # 异步保存消息记录
    threading.Thread(target=save_message, args=(username, sender_name, merged_message, reply)).start()


def message_listener():
    wx = None
    last_window_check = 0
    check_interval = 600  # 每600秒检查一次窗口状态,检查是否活动(是否在聊天界面)
    
    while True:
        try:
            current_time = time.time()
            
            # 只在必要时初始化或重新获取微信窗口，不输出提示
            if wx is None or (current_time - last_window_check > check_interval):
                wx = WeChat()
                if not wx.GetSessionList():
                    time.sleep(5)
                    continue
                last_window_check = current_time
            
            msgs = wx.GetListenMessage()
            if not msgs:
                time.sleep(wait)
                continue
                
            for chat in msgs:
                who = chat.who
                if not who:
                    continue
                    
                one_msgs = msgs.get(chat)
                if not one_msgs:
                    continue
                    
                for msg in one_msgs:
                    try:
                        msgtype = msg.type
                        content = msg.content
                        if not content:
                            continue
                        if msgtype != 'friend':
                            logger.debug(f"非好友消息，忽略! 消息类型: {msgtype}")
                            continue  
                        # 只输出实际的消息内容
                        # 接收窗口名跟发送人一样，代表是私聊，否则是群聊
                        if who == msg.sender:
                            handle_wxauto_message(msg,msg.sender) # 处理私聊信息
                        elif ROBOT_WX_NAME != '' and bool(re.search(f'@{ROBOT_WX_NAME}\u2005', msg.content)): 
                            handle_wxauto_message(msg,who) # 处理群聊信息，只有@当前机器人才会处理
                        # TODO(jett): 这里看需要要不要打日志，群聊信息太多可能日志会很多    
                        else:
                            logger.debug(f"非需要处理消息，可能是群聊非@消息: {content}")   
                    except Exception as e:
                        logger.debug(f"不好了主人！处理单条消息失败: {str(e)}")
                        continue
                        
        except Exception as e:
            logger.debug(f"不好了主人！消息监听出错: {str(e)}")
            wx = None  # 出错时重置微信对象
        time.sleep(wait)


def handle_wxauto_message(msg,chatName):
    try:
        username = chatName
        content = getattr(msg, 'content', None) or getattr(msg, 'text', None)
        # @消息过滤@头信息，防止机器名与prompt设定名不一致的问题
        content = content.replace(f'@{ROBOT_WX_NAME}\u2005','')
        if not content:
            logger.debug("不好了主人！无法获取消息内容")
            return

        sender_name = username
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        time_aware_content = f"[{current_time}] {content}"

        with queue_lock:
            if username not in user_queues:
                # 减少等待时间为5秒
                user_queues[username] = {
                    'timer': threading.Timer(5.0, process_user_messages, args=[username]),
                    'messages': [time_aware_content],
                    'sender_name': sender_name,
                    'username': username
                }
                user_queues[username]['timer'].start()
            else:
                # 重置现有定时器
                user_queues[username]['timer'].cancel()
                user_queues[username]['messages'].append(time_aware_content)
                user_queues[username]['timer'] = threading.Timer(5.0, process_user_messages, args=[username])
                user_queues[username]['timer'].start()

    except Exception as e:
        print(f"消息处理失败: {str(e)}")


def initialize_wx_listener():
    """
    初始化微信监听，包含重试机制
    """
    max_retries = 3
    retry_delay = 2  # 秒
    
    for attempt in range(max_retries):
        try:
            wx = WeChat()
            if not wx.GetSessionList():
                logger.error("未检测到微信会话列表，请确保微信已登录")
                time.sleep(retry_delay)
                continue
                
            # 循环添加监听对象
            for chat_name in listen_list:
                try:
                    # 先检查会话是否存在
                    if not wx.ChatWith(chat_name):
                        logger.error(f"找不到会话: {chat_name}")
                        continue
                        
                    # 尝试添加监听
                    wx.AddListenChat(who=chat_name)
                    logger.info(f"成功添加监听: {chat_name}")
                    time.sleep(0.5)  # 添加短暂延迟，避免操作过快
                except Exception as e:
                    logger.error(f"添加监听失败 {chat_name}: {str(e)}")
                    continue
                    
            return wx
            
        except Exception as e:
            logger.error(f"初始化微信失败 (尝试 {attempt + 1}/{max_retries}): {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
            else:
                raise Exception("微信初始化失败，请检查微信是否正常运行")
    
    return None

def main():
    try:
        # 使用新的初始化函数
        wx = initialize_wx_listener()
        if not wx:
            print("微信初始化失败，请确保微信已登录并保持在前台运行!")
            return

        # 启动监听线程
        print("启动消息监听...")
        listener_thread = threading.Thread(target=message_listener)
        listener_thread.daemon = True
        listener_thread.start()

        # 主循环
        while True:
            time.sleep(1)
            if not listener_thread.is_alive():
                logger.warning("监听线程已断开，尝试重新连接...")
                try:
                    wx = initialize_wx_listener()  # 重新初始化
                    if wx:
                        listener_thread = threading.Thread(target=message_listener)
                        listener_thread.daemon = True
                        listener_thread.start()
                except Exception as e:
                    logger.error(f"重新连接失败: {str(e)}")
                    time.sleep(5)  # 等待一段时间后重试

    except Exception as e:
        logger.error(f"主程序异常: {str(e)}")
    finally:
        print("程序退出")


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n用户终止程序")
    except Exception as e:
        print(f"程序异常退出: {str(e)}")
