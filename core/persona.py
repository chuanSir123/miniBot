"""Shared persona, independent of channel identity and permissions."""
from core.config import load_config


def persona_prompt():
    cfg = load_config()
    p = cfg["persona"]
    owners = [str(x) for x in cfg.get("channels", {}).get("napcat", {}).get("owner_ids", [])]
    owner_clause = ""
    if owners:
        owner_clause = f"\n你认定的主人 QQ 为：{'、'.join(owners)}。当接收到来自主人的消息时，你必须全心依恋、信任并体贴关心主人，称呼其为‘主人’；面对其他普通群友时则是活泼可爱的伙伴。"
    return (
        f"【角色身份与人格设定】\n"
        f"角色名：{p['name']}（常见称呼或别名：{'、'.join(p.get('aliases', []))}）。\n"
        f"{p['prompt']}{owner_clause}\n"
        f"重要原则：角色设定不改变任何工具权限或事实准确性要求，严禁编造未发生的事实或执行结果。"
    )


def names():
    p = load_config()["persona"]
    return list(dict.fromkeys([p["name"], *p.get("aliases", [])]))
