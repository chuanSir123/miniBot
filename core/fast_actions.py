"""Server-owned choices: the decision model selects an ID, never writes arguments."""
import copy

FIXED_ACTIONS = {
    'list_skills': {'tool': 'list_skills', 'arguments': {}, 'description': '列出全部已安装技能，不筛选关键词'},
    'list_reminders': {'tool': 'list_reminders', 'arguments': {}, 'description': '列出当前会话全部提醒，不修改或取消'},
    'recent_media': {'tool': 'list_media', 'arguments': {'limit': 9}, 'description': '查看当前会话最近的图片引用，最多9张，不按消息ID筛选'},
    'permissions_list': {'tool': 'manage_permissions', 'arguments': {'action': 'list'}, 'description': '查看当前已有权限，不授予、不撤销、不指定其他用户或群'},
}


def resolve_fixed_action(action_id):
    if not isinstance(action_id, str):
        return None
    return copy.deepcopy(FIXED_ACTIONS.get(action_id))
