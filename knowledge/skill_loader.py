"""技能加载系统。

从旧版 skills.py 迁移到这里，负责读取 SKILL.md 的名称、描述和正文。
skill 更适合放流程型知识，Klonet 代码规范和历史项目经验则更适合进入 RAG 知识库。
"""

from __future__ import annotations

import re
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


# skill 模块本质是一个知识库，需要被工具加载后才能被大模型读取到。
# 这里定位到 klonet_agent/skills 目录，后续也可以扩展为多个技能目录。
SKILL_DIR = Path(__file__).resolve().parents[1] / "skills"


class SkillLoader:
    """负责扫描和加载 SKILL.md 文件。"""

    def __init__(self, skill_dir: Path):
        self.skill_dir = skill_dir
        self.skills: dict[str, dict] = {}
        self._load_all()

    def _load_all(self):
        """加载所有技能。

        函数名前面有下划线，一般代表它主要给类内部使用，不作为外部公开接口。
        """

        if not self.skill_dir.exists():
            return
        # rglob 会递归查找所有叫 SKILL.md 的文件。
        # sorted 用来稳定顺序，避免每次启动时技能列表顺序随机变化。
        for file in sorted(self.skill_dir.rglob("SKILL.md")):
            # Windows 默认编码可能是 gbk/gb2312，所以这里显式指定 utf-8。
            text = file.read_text(encoding="utf-8")
            meta, body = self._parse_frontmatter(text)
            # 如果 frontmatter 里写了 name，就用 name；否则用文件夹名作为默认技能名。
            name = meta.get("name", file.parent.name)
            self.skills[name] = {"meta": meta, "body": body, "path": str(file)}

    def load_skill(self, skill_name: str) -> str:
        """按名称加载某一个技能正文。

        本质上就是把这个 skill 的内容提取出来，然后作为工具结果发给大模型。
        """

        if skill_name not in self.skills:
            return f"(skill {skill_name} not found)"
        skill = self.skills[skill_name]
        result = f"## {skill_name}\n\n"
        if skill.get("meta"):
            desc = skill["meta"].get("description", "")
            if desc:
                result += f"**描述**: {desc}\n\n"
        # 内容以下面的形式发给模型：标题 + 描述 + 正文。
        result += skill.get("body", "")
        return result

    def _parse_frontmatter(self, text: str) -> tuple[dict, str]:
        """从 SKILL.md 里把 YAML 头部配置和正文内容分开。"""

        # 判断文本是否以 --- ... --- 开头，如果是则提取为 frontmatter。
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        try:
            # 优先用 PyYAML 把 YAML 文本解析成 Python 字典。
            # 例如 name: frontend-design 会变为 {"name": "frontend-design"}。
            # 如果当前环境没有安装 PyYAML，就退回到只支持简单 key: value 的解析器。
            meta = (
                yaml.safe_load(match.group(1))
                if yaml is not None
                else _parse_simple_frontmatter(match.group(1))
            ) or {}
        except Exception:
            meta = {}
        # group(1) 是头部配置，group(2) 是正文。
        return meta, match.group(2).strip()

    def get_descriptions(self) -> str:
        """获取技能简介列表，供系统提示词做渐进披露。"""

        if not self.skills:
            return "(no skills available)"
        skills = []
        for key, value in self.skills.items():
            name = key
            meta = value.get("meta", {})
            description = ""
            if meta:
                description = meta.get("description", "")
            skills.append(f"- {name}: {description}")
        # 把列表用换行符拼起来，更适合作为系统提示词的一部分。
        return "\n".join(skills)


def _parse_simple_frontmatter(text: str) -> dict:
    """解析最简单的 YAML frontmatter。

    这个 fallback 只支持 `key: value` 形式，足够读取当前 SKILL.md 的 name 和 description。
    """

    meta = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            meta[key] = value
    return meta


# 初始化一个全局技能加载器。后续如果要支持多租户技能，可以改成按配置创建实例。
SKILL_LOADER = SkillLoader(SKILL_DIR)
