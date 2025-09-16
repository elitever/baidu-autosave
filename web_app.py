from flask import Flask, request, jsonify, render_template, send_from_directory, session, redirect, url_for
from storage import BaiduStorage
from scheduler import TaskScheduler
import json
from loguru import logger
import sys
import os
import atexit
import re
from functools import wraps
import signal
from utils import generate_transfer_notification
from notify import send as notify_send
from datetime import datetime
from flask_cors import CORS
import time
import socket

from gevent.pywsgi import WSGIServer

# GitHub 仓库信息
GITHUB_REPO = 'kokojacket/baidu-autosave'
# Docker Hub 信息
DOCKER_HUB_RSS = 'https://rsshub.rssforever.com/dockerhub/tag/kokojacket/baidu-autosave'
# 备用 Docker Hub RSS 源
DOCKER_HUB_RSS_ALT = 'https://rss.kuaisouxia.com/dockerhub/tag/kokojacket/baidu-autosave'
# 1ms.run API 源
MS_RUN_API = 'https://1ms.run/api/v1/registry/get_tags'

# 创建日志目录
os.makedirs('log', exist_ok=True)

# 配置日志
logger.remove()  # 移除默认的控制台输出

# 定义统一的日志格式和级别
log_format = "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}"
log_level = "DEBUG"  # 使用DEBUG级别，可以看到所有日志

# 过滤敏感信息
def filter_sensitive_info(record):
    """过滤敏感信息，如BDUSS和cookies"""
    message = record["message"]
    # 替换BDUSS
    message = re.sub(r"BDUSS['\"]?\s*:\s*['\"]?([^'\"]+)['\"]?", "BDUSS: [已隐藏]", message)
    # 替换cookies
    message = re.sub(r"cookies['\"]?\s*:\s*['\"]?([^'\"]+)['\"]?", "cookies: [已隐藏]", message)
    record["message"] = message
    return True

# 过滤轮询请求日志
def filter_polling_requests(record):
    """过滤轮询请求的日志，如/api/tasks/status和/api/logs"""
    message = record["message"]
    
    # 检查是否是HTTP请求日志（WSGI服务器的访问日志）
    if "GET /api/tasks/status HTTP" in message or "GET /api/logs?limit=" in message:
        return False  # 不显示这些日志
    
    return True  # 显示其他所有日志

# 应用过滤器到所有日志处理器
logger.configure(patcher=filter_sensitive_info)

# 添加彩色的控制台输出（带轮询过滤）
logger.add(sys.stdout, 
          level=log_level, 
          format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
          filter=filter_polling_requests)  # 添加轮询过滤器

# 添加文件输出 (内容与控制台输出相同，但没有颜色标记，且不过滤轮询请求)
logger.add("log/web_app_{time:YYYY-MM-DD}.log", 
          rotation="00:00",  # 每天零点创建新文件
          retention="7 days",  # 保留7天的日志
          level=log_level,
          encoding="utf-8",
          format=log_format)

app = Flask(__name__)
app.secret_key = 'your-secret-key-here'  # 用于session加密
CORS(app)

# 全局变量声明
storage = None
scheduler = None


# 登录装饰器
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
            
        # 检查会话是否过期
        auth_config = storage.config.get('auth', {})
        session_timeout = auth_config.get('session_timeout', 3600)
        if time.time() - session.get('login_time', 0) > session_timeout:
            session.clear()
            return redirect(url_for('login'))
            
        # 更新最后活动时间
        session['login_time'] = time.time()
        return f(*args, **kwargs)
    return decorated_function


def init_app():
    """初始化应用"""
    global storage, scheduler
    try:
        logger.info("开始初始化应用...")
        # 初始化存储
        logger.info("正在初始化存储...")
        storage = BaiduStorage()
        
        # 使用已创建的 storage 实例初始化调度器
        try:
            logger.info("正在初始化调度器...")
            scheduler = TaskScheduler(storage)
            scheduler.start()
            logger.info("调度器初始化成功")
        except Exception as e:
            logger.error(f"初始化调度器失败: {str(e)}")
            scheduler = None
        
        if not storage.is_valid():
            logger.warning("存储初始化成功，但未登录或未配置用户")
            
        logger.info("应用初始化完成")
        return True, None
        
    except Exception as e:
        error_msg = f"应用初始化失败: {str(e)}"
        logger.error(error_msg)
        return False, error_msg

def cleanup():
    """清理资源"""
    global scheduler
    if scheduler:
        try:
            if hasattr(scheduler, 'is_running') and scheduler.is_running:
                scheduler.stop()
            scheduler = None
            logger.info("调度器已停止")
        except Exception as e:
            logger.error(f"停止调度器失败: {str(e)}")

def handle_api_error(f):
    """API错误处理装饰器"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            error_msg = f"{f.__name__} 失败: {str(e)}"
            logger.error(error_msg)
            return jsonify({'success': False, 'message': error_msg})
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    """登录处理"""
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if not storage:
            return render_template('login.html', message='系统未初始化')
            
        # 验证用户名和密码
        auth_config = storage.config.get('auth', {})
        if (username == auth_config.get('users') and 
            password == auth_config.get('password')):
            session['username'] = username
            session['login_time'] = time.time()
            
            # 直接重定向到首页，不再等待获取用户配额信息
            return redirect(url_for('index'))
        else:
            return render_template('login.html', message='用户名或密码错误')
            
    return render_template('login.html')

@app.route('/logout')
def logout():
    """登出处理"""
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    """首页"""
    try:
        users = []
        tasks = []
        current_user = None
        config = {}
        login_status = False
        init_error = None
        
        if storage:
            try:
                users = storage.list_users()
                tasks = storage.list_tasks()
                current_user = storage.config['baidu'].get('current_user')
                config = {
                    'cron': storage.config.get('cron', {}),
                    'notify': storage.config.get('notify', {})
                }
                # 同时检查管理员登录状态和百度用户登录状态
                admin_logged_in = 'username' in session
                baidu_logged_in = storage.is_valid()
                login_status = admin_logged_in and baidu_logged_in
                
                # 记录日志
                logger.info(f"登录状态检查 - 管理员: {admin_logged_in}, 百度用户: {baidu_logged_in}, 当前用户: {current_user}")
                
                # 异步获取用户配额信息 - 不阻塞页面加载
                # 用户配额信息将通过API端点/api/user/quota获取
            except Exception as e:
                logger.error(f"获取数据失败: {str(e)}")
                init_error = str(e)
        
        return render_template('index.html', 
                            users=users, 
                            tasks=tasks, 
                            config=config,
                            current_user=current_user,
                            storage=storage,
                            login_status=login_status,
                            init_error=init_error)
    except Exception as e:
        error_msg = f"加载页面失败: {str(e)}"
        logger.error(error_msg)
        return render_template('index.html',
                            users=[],
                            tasks=[],
                            config={},
                            current_user=None,
                            storage=None,
                            login_status=False,
                            init_error=error_msg)

@app.route('/api/tasks', methods=['GET'])
@login_required
@handle_api_error
def get_tasks():
    """获取所有任务"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
    tasks = storage.list_tasks()
    # 按 order 排序，没有 order 的排在最后
    tasks.sort(key=lambda x: x.get('order', float('inf')))
    return jsonify({'success': True, 'tasks': tasks})

@app.route('/api/tasks/<int:task_id>/status', methods=['GET'])
@login_required
@handle_api_error
def get_task_status(task_id):
    """获取单个任务状态"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
    tasks = storage.list_tasks()
    # 按 order 排序，确保 task_id 对应正确的任务
    tasks.sort(key=lambda x: x.get('order', float('inf')))
    if 0 <= task_id < len(tasks):
        return jsonify({'success': True, 'status': tasks[task_id]})
    return jsonify({'success': False, 'message': '任务不存在'})

@app.route('/api/tasks/running', methods=['GET'])
@login_required
@handle_api_error
def get_running_tasks():
    """获取正在运行的任务"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
    tasks = storage.list_tasks()
    # 按 order 排序后再筛选运行中的任务
    tasks.sort(key=lambda x: x.get('order', float('inf')))
    running_tasks = [task for task in tasks if task.get('status') == 'running']
    return jsonify({'success': True, 'tasks': running_tasks})

@app.route('/api/task/add', methods=['POST'])
@login_required
@handle_api_error
def add_task():
    """添加任务"""
    data = request.get_json()
    url = data.get('url', '').strip()
    save_dir = data.get('save_dir', '').strip()
    pwd = data.get('pwd', '').strip()
    name = data.get('name', '').strip()
    cron = data.get('cron', '').strip()
    category = data.get('category', '').strip()
    regex_pattern = data.get('regex_pattern', '').strip()
    regex_replace = data.get('regex_replace', '').strip()
    
    if not url or not save_dir:
        return jsonify({'success': False, 'message': '分享链接和保存目录不能为空'})
    
    # 移除URL中的hash部分
    if '#' in url:
        url = url.split('#')[0]
        
    # 处理URL中的密码部分
    if '?pwd=' in url:
        url, pwd = url.split('?pwd=')
        pwd = pwd.strip()
        
    try:
        # 添加任务 - storage.py 内部会处理调度器更新
        if storage.add_task(url, save_dir, pwd, name, cron, category, regex_pattern, regex_replace):
            
            return jsonify({'success': True, 'message': '添加任务成功'})
            
    except Exception as e:
        logger.error(f"添加任务失败: {str(e)}")
        return jsonify({'success': False, 'message': f'添加任务失败: {str(e)}'})

@app.route('/api/task/update', methods=['POST'])
@login_required
@handle_api_error
def update_task():
    """更新任务"""
    data = request.get_json()
    try:
        task_id = int(data.get('task_id', -1))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': '无效的任务ID'})
    
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    tasks = storage.list_tasks()
    if not tasks:
        return jsonify({'success': False, 'message': '任务列表为空'})
        
    # 按order排序
    tasks.sort(key=lambda x: x.get('order', float('inf')))
    
    # 查找对应order的任务
    task_order = task_id + 1  # task_id 是从0开始的索引，而 order 是从1开始的
    task = None
    for t in tasks:
        if t.get('order') == task_order:
            task = t
            break
            
    if not task:
        return jsonify({'success': False, 'message': f'未找到任务(order={task_order})'})
    
    # 获取新的URL和密码
    new_url = data.get('url', '').strip()
    new_pwd = data.get('pwd', '').strip()
    
    # 如果URL中包含密码部分，提取出来
    if '?pwd=' in new_url:
        new_url, new_pwd = new_url.split('?pwd=')
        new_url = new_url.strip()
        new_pwd = new_pwd.strip()
    
    # 创建更新数据对象，保持原有状态
    update_data = {
        'url': new_url,
        'save_dir': data.get('save_dir', '').strip(),
        'pwd': new_pwd,  # 使用处理后的新密码
        'name': data.get('name', '').strip(),
        'cron': data.get('cron', '').strip(),
        'category': data.get('category', '').strip(),
        'regex_pattern': data.get('regex_pattern', '').strip(),
        'regex_replace': data.get('regex_replace', '').strip(),
        'order': task_order,  # 保持原有的order
        'status': task.get('status', 'normal'),  # 保持原有的状态
        'message': task.get('message', ''),  # 保持原有的消息
        'last_update': int(time.time())  # 添加更新时间戳
    }
    
    # 验证必填字段
    if not update_data['url']:
        return jsonify({'success': False, 'message': '分享链接不能为空'})
    if not update_data['save_dir']:
        return jsonify({'success': False, 'message': '保存目录不能为空'})
        
    try:
        # 更新任务
        success = storage.update_task_by_order(task_order, update_data)
        if not success:
            return jsonify({'success': False, 'message': '更新任务失败'})
        
        
        return jsonify({
            'success': True, 
            'message': '更新任务成功',
            'task': update_data
        })
    except Exception as e:
        logger.error(f"更新任务失败: {str(e)}")
        return jsonify({'success': False, 'message': f'更新任务失败: {str(e)}'})

@app.route('/api/share/info', methods=['POST'])
@login_required
@handle_api_error
def get_share_info():
    """获取分享链接信息"""
    data = request.get_json()
    url = data.get('url', '').strip()
    pwd = data.get('pwd', '').strip()
    
    if not url:
        return jsonify({'success': False, 'message': '分享链接不能为空'})
    
    try:
        # 移除URL中的hash部分
        url = url.split('#')[0]
        
        # 处理URL中的密码部分
        if '?pwd=' in url:
            url, extracted_pwd = url.split('?pwd=')
            pwd = extracted_pwd.strip()
        
        # 获取分享文件信息
        result = storage.get_share_folder_name(url, pwd)
        
        if result['success']:
            return jsonify({
                'success': True,
                'folder_name': result['folder_name'],
                'message': '获取文件夹名称成功'
            })
        else:
            return jsonify({
                'success': False,
                'message': result.get('error', '获取分享信息失败')
            })
            
    except Exception as e:
        logger.error(f"获取分享信息失败: {str(e)}")
        return jsonify({'success': False, 'message': f'获取分享信息失败: {str(e)}'})

@app.route('/api/task/delete', methods=['POST'])
@login_required
@handle_api_error
def delete_task():
    """删除任务"""
    data = request.get_json()
    task_id = data.get('task_id')
    
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
    tasks = storage.list_tasks()
    # 按 order 排序，确保 task_id 对应正确的任务
    tasks.sort(key=lambda x: x.get('order', float('inf')))
    if 0 <= task_id < len(tasks):
        task = tasks[task_id]
        task_order = task.get('order', task_id + 1)
        if storage.remove_task_by_order(task_order):
            # 删除成功后重新整理剩余任务的顺序
            storage._update_task_orders()
            return jsonify({'success': True, 'message': '删除任务成功'})
    return jsonify({'success': False, 'message': '任务不存在'})


@app.route('/api/task/move', methods=['POST'])
@login_required
@handle_api_error
def move_task():
    """移动任务位置"""
    data = request.get_json()
    task_id = data.get('task_id')
    new_index = data.get('new_index')
    
    if task_id is None or new_index is None:
        return jsonify({'success': False, 'message': '缺少必要参数'})
    
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
    
    try:
        tasks = storage.list_tasks()
        # 按 order 排序
        tasks.sort(key=lambda x: x.get('order', float('inf')))
        
        if not (0 <= task_id < len(tasks)) or not (0 <= new_index < len(tasks)):
            return jsonify({'success': False, 'message': '任务ID或位置无效'})
        
        # 移动任务
        task = tasks.pop(task_id)
        tasks.insert(new_index, task)
        
        # 更新所有任务的order
        for i, task in enumerate(tasks):
            task['order'] = i + 1
            storage.update_task(task)
        
        return jsonify({'success': True, 'message': '任务位置已更新'})
        
    except Exception as e:
        logger.error(f"移动任务失败: {str(e)}")
        return jsonify({'success': False, 'message': f'移动任务失败: {str(e)}'})


@app.route('/api/task/execute', methods=['POST'])
@login_required
@handle_api_error
def execute_task():
    """执行指定的任务"""
    data = request.get_json()
    
    # 获取并验证task_id
    try:
        task_id = int(data.get('task_id', -1))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': '无效的任务ID'})
    
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})

    # 获取任务列表
    tasks = storage.list_tasks()
    if not tasks:
        return jsonify({'success': False, 'message': '任务列表为空'})
    
    # 按order排序
    tasks.sort(key=lambda x: x.get('order', float('inf')))
    
    # 查找对应的任务
    task = None
    task_order = task_id + 1  # 前端task_id从0开始，order从1开始
    
    for t in tasks:
        if t.get('order') == task_order:
            task = t
            break
    
    if not task:
        return jsonify({'success': False, 'message': f'未找到任务(order={task_order})'})
    
    task_name = task.get('name') or f'任务{task_order}'
    
    # 更新为运行状态
    storage.update_task_status_by_order(task_order, 'running', '正在执行任务')
    
    try:
        # 重新获取最新的任务数据，确保使用最新的密码等信息
        latest_tasks = storage.list_tasks()
        latest_tasks.sort(key=lambda x: x.get('order', float('inf')))
        latest_task = None
        for t in latest_tasks:
            if t.get('order') == task_order:
                latest_task = t
                break
        
        if not latest_task:
            return jsonify({'success': False, 'message': f'任务已不存在(order={task_order})'})
        
        # 使用最新任务数据
        task = latest_task
        
        def progress_callback(status, message):
            # 进度回调函数，可以在这里记录日志或其他处理
            pass

        result = storage.transfer_share(
            task['url'],
            task.get('pwd'),
            None,
            task.get('save_dir'),
            progress_callback,
            task  # 传入完整的任务配置
        )
        
        if result.get('success'):
            transferred_files = result.get('transferred_files', [])
            if transferred_files:
                task_results = {
                    'success': [task],
                    'failed': [],
                    'transferred_files': {task['url']: transferred_files}
                }
                
                try:
                    # 发送转存成功通知
                    notify_send('百度自动追更', generate_transfer_notification(task_results))
                except Exception as e:
                    logger.error(f"发送转存成功通知失败: {str(e)}")
                
                storage.update_task_status_by_order(
                    task_order, 
                    'normal',
                    '转存成功',
                    transferred_files=transferred_files
                )
                
                
                return jsonify({
                    'success': True, 
                    'message': '任务执行成功',
                    'transferred_files': transferred_files
                })
            else:
                storage.update_task_status_by_order(task_order, 'normal', '没有新文件需要转存')
                return jsonify({'success': True, 'message': '没有新文件需要转存'})
        else:
            error_msg = result.get('error', '转存失败')
            storage.update_task_status_by_order(task_order, 'error', error_msg)
            return jsonify({'success': False, 'message': error_msg})

    except Exception as e:
        error_msg = str(e)
        # 使用存储模块的错误解析功能
        parsed_error = storage._parse_share_error(error_msg) if storage else error_msg
        
        is_share_forbidden = "error_code: 115" in error_msg
        
        if is_share_forbidden:
            try:
                storage.remove_task_by_order(task_order)
                storage._update_task_orders()
            except Exception as del_err:
                pass  # 删除失效任务失败，继续执行
        
        storage.update_task_status_by_order(task_order, 'error', parsed_error)
        return jsonify({'success': False, 'message': parsed_error})

@app.route('/api/users', methods=['GET'])
@login_required
@handle_api_error
def get_users():
    """获取所有用户"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
    
    users = storage.list_users()
    current_username = storage.config.get('baidu', {}).get('current_user')
    
    # 标记当前用户
    for user in users:
        user['is_current'] = user.get('username') == current_username
    
    return jsonify({
        'success': True, 
        'users': users,
        'current_user': current_username
    })

@app.route('/api/user/add', methods=['POST'])
@login_required
@handle_api_error
def add_user():
    """添加用户"""
    data = request.get_json()
    username = data.get('username', '').strip()
    cookies = data.get('cookies', '').strip()
    
    if not username or not cookies:
        return jsonify({'success': False, 'message': '用户名和cookies不能为空'})
        
    if storage.add_user_from_cookies(cookies, username):
        init_app()
        return jsonify({'success': True, 'message': '添加用户成功'})
    return jsonify({'success': False, 'message': '添加用户失败'})

@app.route('/api/user/switch', methods=['POST'])
@login_required
@handle_api_error
def switch_user():
    """切换用户"""
    data = request.get_json()
    username = data.get('username')
    
    if not username:
        return jsonify({'success': False, 'message': '用户名不能为空'})
        
    try:
        if storage.switch_user(username):
            # 获取完整的用户信息
            user = storage.get_user(username)
            if not user:
                return jsonify({'success': False, 'message': f'用户 {username} 不存在'})
            
            # 重新初始化应用
            init_app()
            
            # 切换用户后立即获取用户配额信息
            try:
                if storage and hasattr(storage, 'get_user_info'):
                    user_info = storage.get_user_info()
                    if user_info and 'quota' in user_info:
                        quota = user_info['quota']
                        total_gb = round(quota.get('total', 0) / (1024**3), 2)
                        used_gb = round(quota.get('used', 0) / (1024**3), 2)
                        logger.info(f"已切换到用户: {username}，网盘总空间: {total_gb}GB, 已使用: {used_gb}GB")
                        
                        # 将配额信息添加到用户数据中
                        user['quota'] = {
                            'total': quota.get('total', 0),
                            'used': quota.get('used', 0),
                            'total_gb': total_gb,
                            'used_gb': used_gb,
                            'percent': round(quota.get('used', 0) / quota.get('total', 1) * 100, 2) if quota.get('total', 0) > 0 else 0
                        }
            except Exception as e:
                logger.error(f"切换用户后获取配额信息失败: {str(e)}")
            
            # 返回更新后的状态
            return jsonify({
                'success': True, 
                'message': '切换用户成功',
                'current_user': user,
                'login_status': storage.is_valid()
            })
        return jsonify({'success': False, 'message': '切换用户失败'})
    except Exception as e:
        logger.error(f"切换用户失败: {str(e)}")
        return jsonify({'success': False, 'message': f'切换用户失败: {str(e)}'})

@app.route('/api/user/delete', methods=['POST'])
@login_required
@handle_api_error
def delete_user():
    """删除用户"""
    data = request.get_json()
    username = data.get('username')
    
    if not username:
        return jsonify({'success': False, 'message': '用户名不能为空'})
        
    current_user = storage.config['baidu'].get('current_user')
    if current_user == username:
        return jsonify({'success': False, 'message': '不能删除当前使用的用户'})
        
    if storage.remove_user(username):
        return jsonify({'success': True, 'message': '删除用户成功'})
    return jsonify({'success': False, 'message': '删除用户失败'})

@app.route('/api/user/update', methods=['POST'])
@login_required
@handle_api_error
def update_user():
    """更新用户信息"""
    data = request.get_json()
    original_username = data.get('original_username', '').strip()
    username = data.get('username', '').strip()
    cookies = data.get('cookies', '').strip()
    
    if not original_username or not username or not cookies:
        return jsonify({'success': False, 'message': '原始用户名、新用户名和cookies不能为空'})
    
    # 如果是重命名用户
    if original_username != username:
        # 检查新用户名是否已存在
        if username in storage.config['baidu']['users']:
            return jsonify({'success': False, 'message': f'用户名 {username} 已存在'})
        
        # 获取原用户信息
        user_info = storage.get_user(original_username)
        if not user_info:
            return jsonify({'success': False, 'message': f'用户 {original_username} 不存在'})
        
        # 检查cookies是否发生变化
        cookies_changed = user_info.get('cookies', '') != cookies
        
        # 如果仅重命名，无需验证cookies
        if not cookies_changed:
            # 复制用户信息到新用户名
            storage.config['baidu']['users'][username] = storage.config['baidu']['users'][original_username].copy()
            
            # 如果是当前用户，更新当前用户名
            if storage.config['baidu']['current_user'] == original_username:
                storage.config['baidu']['current_user'] = username
            
            # 删除原用户
            storage.remove_user(original_username)
            
            # 保存配置
            storage._save_config()
            
            return jsonify({'success': True, 'message': '用户更新成功'})
        else:
            # 创建新用户
            if storage.add_user_from_cookies(cookies, username):
                # 如果是当前用户，更新当前用户名
                if storage.config['baidu']['current_user'] == original_username:
                    storage.switch_user(username)
                
                # 删除原用户
                storage.remove_user(original_username)
                
                return jsonify({'success': True, 'message': '用户更新成功'})
            else:
                return jsonify({'success': False, 'message': '用户更新失败，cookies可能无效'})
    else:
        # 仅更新cookies
        if storage.update_user(username, cookies):
            init_app()
            return jsonify({'success': True, 'message': '用户更新成功'})
        return jsonify({'success': False, 'message': '用户更新失败，cookies可能无效'})

@app.route('/api/user/<username>/cookies', methods=['GET'])
@login_required
@handle_api_error
def get_user_cookies(username):
    """获取用户cookies"""
    user_info = storage.get_user(username)
    if not user_info:
        return jsonify({'success': False, 'message': f'用户 {username} 不存在'})
    
    return jsonify({'success': True, 'cookies': user_info.get('cookies', '')})

@app.route('/api/user/quota', methods=['GET'])
@login_required
@handle_api_error
def get_user_quota():
    """获取当前用户的网盘配额信息"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    try:
        # 获取用户信息，包括配额
        user_info = storage.get_user_info()
        if not user_info or 'quota' not in user_info:
            return jsonify({'success': False, 'message': '无法获取用户配额信息'})
            
        # 提取配额信息
        quota = user_info['quota']
        total = quota.get('total', 0)
        used = quota.get('used', 0)
        
        # 转换为GB并保留2位小数
        total_gb = round(total / (1024**3), 2)
        used_gb = round(used / (1024**3), 2)
        
        return jsonify({
            'success': True, 
            'quota': {
                'total': total,
                'used': used,
                'total_gb': total_gb,
                'used_gb': used_gb,
                'percent': round(used / total * 100, 2) if total > 0 else 0
            }
        })
    except Exception as e:
        logger.error(f"获取用户配额失败: {str(e)}")
        return jsonify({'success': False, 'message': f'获取用户配额失败: {str(e)}'})

@app.route('/api/config', methods=['GET'])
@login_required
@handle_api_error
def get_config():
    """获取配置"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
    
    # 获取当前用户的完整信息
    current_user = None
    current_username = storage.config.get('baidu', {}).get('current_user')
    if current_username:
        current_user = storage.get_user(current_username)
    
    config = {
        'cron': storage.config.get('cron', {}),
        'notify': storage.config.get('notify', {}),
        'scheduler': storage.config.get('scheduler', {}),
        'quota_alert': storage.config.get('quota_alert', {}),
        'share': storage.config.get('share', {}),
        'file_operations': storage.config.get('file_operations', {}),
        'baidu': {
            'current_user': current_user  # 返回完整的用户信息
        }
    }
    return jsonify({'success': True, 'config': config})

def format_webhook_body(webhook_body):
    """格式化WEBHOOK_BODY字段，将简化格式转换为标准多行格式"""
    if not webhook_body or isinstance(webhook_body, dict):
        return webhook_body
    
    # 检测是否是简化格式（如：title: "$title"content: "$content"source: "我的项目"）
    import re
    simple_format = re.match(r'title:\s*"([^"]*)"content:\s*"([^"]*)"source:\s*"([^"]*)"', webhook_body)
    
    if simple_format:
        # 转换为标准多行格式
        title = simple_format.group(1)
        content = simple_format.group(2)
        source = simple_format.group(3)
        return f'title: "{title}"\ncontent: "{content}"\nsource: "{source}"'
    
    # 如果不是简化格式，直接返回原始值
    return webhook_body

@app.route('/api/config/update', methods=['POST'])
@login_required
@handle_api_error
def update_config():
    """更新配置"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    data = request.get_json()
    
    # 自动格式化WEBHOOK_BODY字段
    if 'notify' in data and 'direct_fields' in data['notify']:
        direct_fields = data['notify']['direct_fields']
        if 'WEBHOOK_BODY' in direct_fields:
            direct_fields['WEBHOOK_BODY'] = format_webhook_body(direct_fields['WEBHOOK_BODY'])
    
    storage.config.update(data)
    storage._save_config()
    
    # 处理调度器配置更新
    if scheduler and ('cron' in data or 'scheduler' in data):
        try:
            was_running = scheduler.is_running
            if was_running:
                scheduler.stop()
                logger.info('调度器已停止')
            
            # 重新初始化调度器
            scheduler._init_scheduler()
            
            # 如果之前在运行，或者配置中指定了自动启动，则启动调度器
            should_start = was_running or data.get('cron', {}).get('auto_install', True)
            if should_start and not scheduler.is_running:
                scheduler.start()
                logger.info('调度器已重新启动')
            
            logger.info('调度器配置已更新')
        except Exception as e:
            logger.error(f'更新调度器配置失败: {str(e)}')
            return jsonify({
                'success': False,
                'message': f'配置已保存，但更新调度器失败: {str(e)}'
            })
    
    # 处理通知配置更新
    if scheduler and 'notify' in data:
        try:
            # 重新初始化通知配置
            scheduler._init_notify()
            logger.info('通知配置已更新')
        except Exception as e:
            logger.error(f'更新通知配置失败: {str(e)}')
            return jsonify({
                'success': False,
                'message': f'配置已保存，但更新通知配置失败: {str(e)}'
            })
    
    return jsonify({'success': True, 'message': '更新配置成功'})

@app.route('/api/notify/test', methods=['POST'])
@login_required
@handle_api_error
def test_notify():
    """测试通知功能"""
    if not storage or not storage.config.get('notify', {}).get('enabled'):
        return jsonify({'success': False, 'message': '通知功能未启用'})
        
    try:
        # 确保通知配置正确加载
        notify_config = storage.config.get('notify', {})
        if notify_config and notify_config.get('enabled'):
            # 重新应用通知配置
            from notify import push_config, send as notify_send
            
            # 应用直接字段配置
            if 'direct_fields' in notify_config:
                for key, value in notify_config.get('direct_fields', {}).items():
                    push_config[key] = value
            # 兼容旧版配置
            elif 'channels' in notify_config and 'pushplus' in notify_config['channels']:
                pushplus = notify_config['channels']['pushplus']
                if 'token' in pushplus:
                    push_config['PUSH_PLUS_TOKEN'] = pushplus['token']
                if 'topic' in pushplus:
                    push_config['PUSH_PLUS_USER'] = pushplus['topic']
            
            # 应用自定义字段
            if 'custom_fields' in notify_config:
                for key, value in notify_config.get('custom_fields', {}).items():
                    push_config[key] = value
            
            # 使用时间戳确保每次内容不同，避免重复内容限制
            import time
            timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
            
            # 使用notify_send发送通知
            notify_send('百度网盘自动追更', f'这是一条测试通知,如果你收到了这条消息,说明通知配置正确! 测试时间: {timestamp}')
            
            return jsonify({'success': True, 'message': '测试通知已发送'})
        else:
            return jsonify({'success': False, 'message': '通知功能未启用'})
    except Exception as e:
        logger.error(f"发送测试通知失败: {str(e)}")
        return jsonify({'success': False, 'message': f'发送测试通知失败: {str(e)}'})

@app.route('/api/tasks/execute-all', methods=['POST'])
@login_required
@handle_api_error
def execute_all_tasks():
    """批量执行任务"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    data = request.get_json()
    task_ids = data.get('task_ids', [])
    
    if not task_ids:
        return jsonify({'success': False, 'message': '没有指定要执行的任务'})
    
    # 将task_ids转换为orders
    task_orders = [task_id + 1 for task_id in task_ids]
    
    # 获取并按order排序的任务列表
    tasks = storage.list_tasks()
    if not tasks:
        return jsonify({'success': False, 'message': '任务列表为空'})
    
    tasks.sort(key=lambda x: x.get('order', float('inf')))
    
    # 找出要执行的任务
    selected_tasks = [task for task in tasks if task.get('order') in task_orders]
    
    if not selected_tasks:
        return jsonify({'success': False, 'message': '未找到指定的任务'})
    
    results = {
        'success': [],
        'skipped': [],
        'failed': [],
        'transferred_files': {}
    }
    
    for task in selected_tasks:
        task_order = task.get('order')
        if not task_order:
            continue
            
        try:
            result = storage.transfer_share(
                task['url'],
                task.get('pwd'),
                None,
                task.get('save_dir'),
                None,  # progress_callback
                task   # task_config
            )
            
            if result.get('success'):
                if result.get('skipped'):
                    results['skipped'].append(task)
                    storage.update_task_status_by_order(task_order, 'skipped', '没有新文件需要转存')
                else:
                    transferred_files = result.get('transferred_files', [])
                    if transferred_files:
                        results['success'].append(task)
                        results['transferred_files'][task['url']] = transferred_files
                        storage.update_task_status_by_order(
                            task_order, 
                            'success',
                            '转存成功',
                            transferred_files=transferred_files
                        )
                    else:
                        results['skipped'].append(task)
                        storage.update_task_status_by_order(task_order, 'skipped', '没有新文件需要转存')
            else:
                error_msg = result.get('error', '转存失败')
                results['failed'].append(task)
                storage.update_task_status_by_order(task_order, 'failed', error_msg)
                
        except Exception as e:
            error_msg = str(e)
            if "error_code: 115" in error_msg:
                error_msg = "该分享链接已失效（文件禁止分享）"
                try:
                    storage.remove_task_by_order(task_order)
                except Exception as del_err:
                    logger.error(f"删除失效任务失败: {str(del_err)}")
            results['failed'].append(task)
            storage.update_task_status_by_order(task_order, 'failed', error_msg)
    
    # 发送通知
    if results['success'] or results['failed']:
        try:
            notification_content = generate_transfer_notification(results)
            notify_send("百度网盘自动追更", notification_content)
        except Exception as e:
            logger.error(f"发送通知失败: {str(e)}")
    
    return jsonify({
        'success': True,
        'message': f'批量执行完成，成功: {len(results["success"])}，跳过: {len(results["skipped"])}，失败: {len(results["failed"])}',
        'results': results
    })

@app.route('/api/categories', methods=['GET'])
@login_required
@handle_api_error
def get_categories():
    """获取所有任务分类"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
    categories = storage.get_task_categories()
    return jsonify({'success': True, 'categories': categories})

@app.route('/api/tasks/category/<category>', methods=['GET'])
@login_required
@handle_api_error
def get_tasks_by_category(category):
    """获取指定分类的任务"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    if category == 'uncategorized':
        tasks = storage.get_tasks_by_category(None)
    else:
        tasks = storage.get_tasks_by_category(category)
    
    # 按 order 排序
    tasks.sort(key=lambda x: x.get('order', float('inf')))
    return jsonify({'success': True, 'tasks': tasks})

@app.route('/api/notify/fields', methods=['POST'])
@login_required
@handle_api_error
def add_notify_field():
    """添加自定义通知字段"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    data = request.get_json()
    field_name = data.get('name', '').strip()
    field_value = data.get('value', '').strip()
    
    if not field_name:
        return jsonify({'success': False, 'message': '字段名称不能为空'})
    
    # 自动格式化WEBHOOK_BODY字段
    if field_name == 'WEBHOOK_BODY':
        field_value = format_webhook_body(field_value)
        
    notify_config = storage.config.get('notify', {})
    if 'custom_fields' not in notify_config:
        notify_config['custom_fields'] = {}
        
    notify_config['custom_fields'][field_name] = field_value
    storage.config['notify'] = notify_config
    storage._save_config()
    
    return jsonify({'success': True, 'message': '添加通知字段成功'})

@app.route('/api/notify/fields', methods=['DELETE'])
@login_required
@handle_api_error
def delete_notify_field():
    """删除通知字段"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    data = request.get_json()
    field_name = data.get('name', '').strip()
    
    if not field_name:
        return jsonify({'success': False, 'message': '字段名称不能为空'})
        
    notify_config = storage.config.get('notify', {})
    
    # 检查字段在哪个配置中
    field_deleted = False
    
    # 1. 检查direct_fields
    if 'direct_fields' in notify_config and field_name in notify_config['direct_fields']:
        del notify_config['direct_fields'][field_name]
        field_deleted = True
    
    # 2. 检查custom_fields (兼容旧版本)
    if not field_deleted and 'custom_fields' in notify_config and field_name in notify_config['custom_fields']:
        del notify_config['custom_fields'][field_name]
        field_deleted = True
    
    if not field_deleted:
        return jsonify({'success': False, 'message': f'未找到字段: {field_name}'})
    
    storage.config['notify'] = notify_config
    storage._save_config()
    
    # 重新初始化通知配置
    if scheduler:
        scheduler._init_notify()
    
    return jsonify({'success': True, 'message': f'字段 {field_name} 已删除'})

@app.route('/api/task/reorder', methods=['POST'])
@login_required
@handle_api_error
def reorder_task():
    """重新排序任务"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    data = request.get_json()
    task_id = data.get('task_id')
    new_order = data.get('new_order')
    
    if task_id is None or new_order is None:
        return jsonify({'success': False, 'message': '任务ID和新顺序不能为空'})
    
    # 将task_id转换为order
    task_order = task_id + 1
        
    if storage.reorder_task(task_order, new_order):
        return jsonify({'success': True, 'message': '任务重排序成功'})
    return jsonify({'success': False, 'message': '任务重排序失败'})


# 静态文件路由
@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('static', path)

# 错误处理
@app.errorhandler(404)
def not_found(error):
    return jsonify({'success': False, 'message': '接口不存在'}), 404

@app.errorhandler(405)
def method_not_allowed(error):
    return jsonify({'success': False, 'message': '请求方法不允许'}), 405

@app.errorhandler(500)
def internal_error(error):
    return jsonify({'success': False, 'message': '服务器内部错误'}), 500

def signal_handler(signum, frame):
    """处理退出信号"""
    logger.info("接收到退出信号，正在清理...")
    try:
        cleanup()
    except Exception as e:
        logger.error(f"清理过程出错: {str(e)}")
    finally:
        logger.info("清理完成，正在退出...")
        sys.exit(0)

@app.route('/api/scheduler/reload', methods=['POST'])
@login_required
def reload_scheduler():
    """重新加载调度器，使配置更改生效"""
    try:
        was_running = scheduler.is_running
        if was_running:
            scheduler.stop()
            logger.info('调度器已停止')
        
        # 重新初始化调度器
        scheduler._init_scheduler()
        
        # 如果之前在运行，则重新启动
        if was_running and not scheduler.is_running:
            scheduler.start()
            logger.info('调度器已重新启动')
        
        logger.info('调度器已重新加载')
        return jsonify({
            'success': True,
            'message': '调度器已重新加载'
        })
    except Exception as e:
        logger.error(f'重新加载调度器失败: {str(e)}')
        return jsonify({
            'success': False,
            'message': f'重新加载调度器失败: {str(e)}'
        }), 500

@app.route('/api/tasks/batch-delete', methods=['POST'])
@login_required
@handle_api_error
def batch_delete_tasks():
    """批量删除任务"""
    data = request.get_json()
    task_ids = data.get('task_ids', [])
    
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    if not task_ids:
        return jsonify({'success': False, 'message': '没有指定要删除的任务'})
    
    try:
        # 将task_ids转换为orders（task_id + 1）
        task_orders = [task_id + 1 for task_id in task_ids]
        
        # 批量删除任务
        deleted_count = storage.remove_tasks(task_orders)
        
        if deleted_count > 0:
            return jsonify({
                'success': True,
                'message': f'成功删除{deleted_count}个任务'
            })
        else:
            return jsonify({
                'success': False,
                'message': '没有任务被删除'
            })
            
    except Exception as e:
        error_msg = str(e)
        logger.error(f"批量删除任务失败: {error_msg}")
        return jsonify({
            'success': False,
            'message': f'批量删除任务失败: {error_msg}'
        })

@app.route('/api/auth/login', methods=['POST'])
@handle_api_error
def api_login():
    """API登录接口"""
    username = request.json.get('username') if request.is_json else request.form.get('username')
    password = request.json.get('password') if request.is_json else request.form.get('password')
    
    if not storage:
        return jsonify({'success': False, 'message': '系统未初始化'}), 400
        
    # 验证用户名和密码
    auth_config = storage.config.get('auth', {})
    if (username == auth_config.get('users') and 
        password == auth_config.get('password')):
        session['username'] = username
        session['login_time'] = time.time()
        
        return jsonify({
            'success': True, 
            'message': '登录成功',
            'username': username
        })
    else:
        return jsonify({'success': False, 'message': '用户名或密码错误'}), 401

@app.route('/api/auth/logout', methods=['POST'])
@handle_api_error
def api_logout():
    """API登出接口"""
    session.clear()
    return jsonify({'success': True, 'message': '登出成功'})

@app.route('/api/auth/check', methods=['GET'])
@handle_api_error
def api_check_auth():
    """检查认证状态"""
    if 'username' not in session:
        return jsonify({'success': False, 'message': '未登录'}), 401
        
    # 检查会话是否过期
    auth_config = storage.config.get('auth', {}) if storage else {}
    session_timeout = auth_config.get('session_timeout', 3600)
    if time.time() - session.get('login_time', 0) > session_timeout:
        session.clear()
        return jsonify({'success': False, 'message': '会话已过期'}), 401
        
    return jsonify({
        'success': True, 
        'message': '认证有效',
        'username': session['username']
    })

@app.route('/api/auth/update', methods=['POST'])
@login_required
@handle_api_error
def update_auth():
    """更新登录凭据"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    data = request.get_json()
    new_username = data.get('username', '').strip()
    new_password = data.get('password', '').strip()
    old_password = data.get('old_password', '').strip()
    
    if not new_username or not new_password or not old_password:
        return jsonify({'success': False, 'message': '用户名、新密码和旧密码都不能为空'})
    
    # 验证旧密码
    auth_config = storage.config.get('auth', {})
    if old_password != auth_config.get('password'):
        return jsonify({'success': False, 'message': '旧密码错误'})
    
    # 更新配置
    auth_config['users'] = new_username
    auth_config['password'] = new_password
    storage.config['auth'] = auth_config
    storage._save_config()
    
    return jsonify({'success': True, 'message': '登录凭据更新成功'})

@app.route('/api/version/check', methods=['GET'])
@login_required
@handle_api_error
def check_version():
    """检查最新版本"""
    try:
        import feedparser
        import requests
        from requests.exceptions import RequestException
        import re
        
        # 获取查询参数，确定使用哪个源检查更新
        source = request.args.get('source', 'github')
        
        if source == 'dockerhub':
            # 使用 Docker Hub RSS 检查更新
            feed_url = DOCKER_HUB_RSS
        elif source == 'dockerhub_alt':
            # 使用备用 Docker Hub RSS 源
            feed_url = DOCKER_HUB_RSS_ALT
        elif source in ['msrun', '1ms']:
            # 使用 1ms.run API 获取版本信息
            try:
                params = {
                    "repositories": "kokojacket/baidu-autosave",
                    "page": 1,
                    "page_size": 10,
                    "search": ""
                }
                response = requests.get(MS_RUN_API, params=params, timeout=5)
                response.raise_for_status()
                data = response.json()
                
                if data.get('code') == 0 and data.get('data', {}).get('list'):
                    # 查找最新的正式版本（格式为vX.Y.Z）
                    version_tags = []
                    latest_tag = None
                    
                    # 首先找到latest标签
                    for tag_info in data['data']['list']:
                        if tag_info['tag_name'] == 'latest':
                            latest_tag = tag_info
                            break
                    
                    if latest_tag:
                        # 找到与latest标签具有相同digest的版本标签
                        latest_digest = latest_tag.get('digest')
                        for tag_info in data['data']['list']:
                            if re.match(r'^v\d+\.\d+\.\d+$', tag_info['tag_name']) and tag_info.get('digest') == latest_digest:
                                version_tags.append(tag_info)
                    
                    # 如果没有找到与latest相同digest的版本标签，则收集所有版本标签
                    if not version_tags:
                        for tag_info in data['data']['list']:
                            if re.match(r'^v\d+\.\d+\.\d+$', tag_info['tag_name']):
                                version_tags.append(tag_info)
                    
                    if version_tags:
                        # 按更新时间排序，选择最新的
                        version_tags.sort(key=lambda x: x.get('tag_last_pushed', ''), reverse=True)
                        latest_version = version_tags[0]['tag_name']
                        published = version_tags[0].get('tag_last_pushed')
                        link = f"https://hub.docker.com/layers/kokojacket/baidu-autosave/{latest_version}/images/{version_tags[0].get('digest', '').split(':')[-1]}"
                        
                        logger.info(f"从1ms.run API获取到最新版本: {latest_version}")
                        return jsonify({
                            'success': True,
                            'version': latest_version,
                            'published': published,
                            'link': link,
                            'source': '1ms'
                        })
                
                # 如果没有找到有效的版本信息，返回错误
                logger.warning("1ms.run API未返回有效的版本信息")
                return jsonify({
                    'success': False,
                    'message': '1ms.run API未返回有效的版本信息',
                    'source': '1ms'
                })
                
            except Exception as e:
                logger.warning(f"从1ms.run API获取版本信息失败: {str(e)}")
                return jsonify({
                    'success': False,
                    'message': f'从1ms.run API获取版本信息失败: {str(e)}',
                    'source': '1ms'
                })
        else:
            # 默认使用 GitHub releases feed
            feed_url = f'https://github.com/{GITHUB_REPO}/releases.atom'
        
        # 如果是使用RSS源，则执行以下代码
        if source in ['github', 'dockerhub', 'dockerhub_alt']:
            try:
                # 设置超时，避免长时间等待
                response = requests.get(feed_url, timeout=5)
                response.raise_for_status()  # 如果响应状态码不是200，抛出异常
            except RequestException as e:
                logger.warning(f"获取{source}版本信息失败: {str(e)}")
                return jsonify({
                    'success': False,
                    'message': f'无法获取{source}版本信息: {str(e)}',
                    'source': source
                })
                
            # 解析 feed
            feed = feedparser.parse(response.content)
            if not feed.entries:
                logger.warning(f"{source}未找到版本信息")
                return jsonify({
                    'success': False,
                    'message': f'{source}未找到版本信息',
                    'source': source
                })
                
            # 获取最新版本信息
            if source in ['dockerhub', 'dockerhub_alt']:
                # 首先查找latest标签的条目
                latest_entry = None
                latest_guid = None
                version_entry = None
                
                for entry in feed.entries:
                    if ':latest' in entry.title:
                        latest_entry = entry
                        # 提取镜像ID（guid的@后面部分）
                        guid_match = re.search(r'@([a-f0-9]+)$', entry.guid)
                        if guid_match:
                            latest_guid = guid_match.group(1)
                        break
                
                if not latest_entry:
                    logger.warning("Docker Hub中未找到latest标签")
                    # 如果没有找到latest标签，使用第一个条目
                    latest_entry = feed.entries[0]
                
                # 如果找到了latest的guid，查找对应的版本号条目
                if latest_guid:
                    for entry in feed.entries:
                        # 检查是否是版本号标签（如v1.0.8）并且与latest有相同的guid
                        if re.search(r':v\d+\.\d+\.\d+', entry.title) and latest_guid in entry.guid:
                            version_entry = entry
                            break
                
                # 如果找到了版本号条目，使用它；否则使用latest条目
                entry_to_use = version_entry if version_entry else latest_entry
                
                # 提取版本号
                title = entry_to_use.title
                version_match = re.search(r':(?:v?\d+\.\d+\.\d+|latest)', title)
                latest_version = version_match.group(0)[1:] if version_match else title
                
                # 添加发布日期
                pub_date = entry_to_use.pubDate if hasattr(entry_to_use, 'pubDate') else entry_to_use.published
                
                logger.info(f"从Docker Hub ({source})获取到最新版本: {latest_version}")
                return jsonify({
                    'success': True,
                    'version': latest_version,
                    'published': pub_date,
                    'link': entry_to_use.link,
                    'source': source
                })
            else:
                # GitHub 格式
                title = feed.entries[0].title
                
                # 从标题中提取版本号，支持多种格式：
                # 1. "Release v1.0.8" -> "v1.0.8"
                # 2. "v1.0.8" -> "v1.0.8"
                # 3. "1.0.8" -> "1.0.8"
                version_match = re.search(r'(?:Release\s+)?(v?\d+\.\d+\.\d+)', title)
                if version_match:
                    latest_version = version_match.group(1)
                    # 确保版本号以v开头
                    if not latest_version.startswith('v'):
                        latest_version = 'v' + latest_version
                else:
                    # 如果无法提取版本号，使用原始标题
                    latest_version = title
                
                pub_date = feed.entries[0].published if hasattr(feed.entries[0], 'published') else None
                link = feed.entries[0].link if hasattr(feed.entries[0], 'link') else f"https://github.com/{GITHUB_REPO}/releases/latest"
                
                logger.info(f"从GitHub获取到最新版本: {title} -> 提取版本号: {latest_version}")
                return jsonify({
                    'success': True,
                    'version': latest_version,
                    'published': pub_date,
                    'link': link,
                    'source': 'github'
                })
    except Exception as e:
        logger.error(f"检查版本失败: {str(e)}")
        return jsonify({
            'success': False,
            'message': f'检查版本失败: {str(e)}',
            'source': source if 'source' in locals() else 'unknown'
        })

# 添加轮询API端点
@app.route('/api/tasks/status', methods=['GET'])
@login_required
@handle_api_error
def get_tasks_status():
    """获取所有任务的状态（用于轮询）"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
    tasks = storage.list_tasks()
    # 按 order 排序，没有 order 的排在最后
    tasks.sort(key=lambda x: x.get('order', float('inf')))
    return jsonify({'success': True, 'tasks': tasks})

@app.route('/api/logs', methods=['GET'])
@login_required
@handle_api_error
def get_logs():
    """获取最近的日志（用于轮询）"""
    # 获取查询参数
    limit = request.args.get('limit', 20, type=int)
    
    # 从日志文件中读取最新的日志
    log_entries = []
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        log_file = f"log/web_app_{today}.log"
        
        if os.path.exists(log_file):
            with open(log_file, 'r', encoding='utf-8') as f:
                # 读取最后limit行
                lines = f.readlines()
                last_lines = lines[-limit:] if len(lines) > limit else lines
                
                for line in last_lines:
                    # 解析日志格式
                    try:
                        parts = line.split('|')
                        if len(parts) >= 3:
                            timestamp = parts[0].strip()
                            level = parts[1].strip()
                            message = '|'.join(parts[2:]).strip()
                            
                            log_entries.append({
                                'timestamp': timestamp,
                                'level': level,
                                'message': message
                            })
                    except:
                        # 如果解析失败，添加原始行
                        log_entries.append({
                            'timestamp': '',
                            'level': 'INFO',
                            'message': line.strip()
                        })
    except Exception as e:
        logger.error(f"读取日志文件失败: {str(e)}")
        return jsonify({'success': False, 'message': f'读取日志文件失败: {str(e)}'})
    
    return jsonify({'success': True, 'logs': log_entries})

@app.route('/api/task/log/<int:task_id>', methods=['GET'])
@login_required
@handle_api_error
def get_task_log(task_id):
    """获取指定任务的执行日志（用于轮询）"""
    # 这里我们返回一个空列表，因为我们没有为每个任务单独存储日志
    # 在实际应用中，可以考虑为每个任务添加日志存储
    return jsonify({'success': True, 'logs': []})

@app.route('/api/task/share', methods=['POST'])
@login_required
@handle_api_error
def share_task():
    """生成任务的分享链接"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
    
    data = request.get_json()
    task_id = data.get('task_id')
    custom_password = data.get('password')  # 可选的自定义密码
    custom_period = data.get('period')      # 可选的自定义有效期
    
    if task_id is None:
        return jsonify({'success': False, 'message': '任务ID不能为空'})
    
    # 获取任务信息
    tasks = storage.list_tasks()
    tasks.sort(key=lambda x: x.get('order', float('inf')))
    
    if 0 <= task_id < len(tasks):
        task = tasks[task_id]
        save_dir = task.get('save_dir')
        
        if not save_dir:
            return jsonify({'success': False, 'message': '任务保存目录为空'})
        
        # 获取分享配置
        share_config = storage.config.get('share', {})
        password = custom_password or share_config.get('default_password', '1234')
        period_days = custom_period or share_config.get('default_period_days', 7)
        
        try:
            # 调用BaiduPCS-Py的share命令
            # 注意：share_file函数内部会检查并创建目录
            share_result = storage.share_file(save_dir, password, period_days)
            
            if share_result.get('success'):
                share_info = share_result.get('share_info', {})
                
                # 更新任务的分享信息
                task_order = task.get('order', task_id + 1)
                storage.update_task_share_info(task_order, share_info)
                
                return jsonify({
                    'success': True, 
                    'message': '分享链接生成成功',
                    'share_info': share_info
                })
            else:
                return jsonify({
                    'success': False, 
                    'message': share_result.get('error', '分享链接生成失败')
                })
                
        except Exception as e:
            logger.error(f"生成分享链接失败: {str(e)}")
            return jsonify({'success': False, 'message': f'生成分享链接失败: {str(e)}'})
    else:
        return jsonify({'success': False, 'message': '任务不存在'})

@app.route('/api/config/share', methods=['POST'])
@login_required
@handle_api_error
def update_share_config():
    """更新分享配置"""
    if not storage:
        return jsonify({'success': False, 'message': '存储未初始化'})
        
    data = request.get_json()
    share_config = {
        'default_password': data.get('default_password', '1234'),
        'default_period_days': data.get('default_period_days', 7)
    }
    
    # 更新配置
    storage.config['share'] = share_config
    storage._save_config()
    
    return jsonify({'success': True, 'message': '分享配置已更新'})

if __name__ == '__main__':
    try:
        # 启动时初始化应用
        init_success, init_error = init_app()
        if not init_success:
            logger.error(f"应用初始化失败: {init_error}")
            if init_error:
                logger.warning("将继续启动 Web 界面，但部分功能可能不可用")
        
        # 注册信号处理器
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # 启动HTTP服务器
        logger.info("使用标准WSGI服务器")
        http_server = WSGIServer(('0.0.0.0', 5000), app, log=None)  # 禁用访问日志
            
        print('Server started at http://0.0.0.0:5000')
        http_server.serve_forever()
    except KeyboardInterrupt:
        logger.info("接收到 Ctrl+C，正在退出...")
        signal_handler(signal.SIGINT, None)
    except Exception as e:
        logger.error(f"应用运行出错: {str(e)}")
        try:
            signal_handler(signal.SIGTERM, None)
        except:
            sys.exit(1) 