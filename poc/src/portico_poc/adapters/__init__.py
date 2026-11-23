"""把业务系统的能力接入 Portico 的适配器。

每个适配器都要回答契约里的演员传播（actor_propagation）与数据范围（data_scope）
到底怎么落地。写不出答案的，说明那个系统的权限模型还没摸清楚。
"""

from .base import Adapter, AdapterRegistry, ExecutionContext

__all__ = ["Adapter", "AdapterRegistry", "ExecutionContext"]
