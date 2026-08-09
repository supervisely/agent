from abc import ABC, abstractmethod
from typing import Dict, Generator, List, Literal, Optional


class BaseContainerExec(ABC):
    @abstractmethod
    def stream_logs(self) -> Generator[str, None, None]:
        raise NotImplementedError()

    @abstractmethod
    def get_exit_code(self) -> Optional[int]:
        raise NotImplementedError()


class BaseContainer(ABC):
    @abstractmethod
    def stop(self, *, timeout: Optional[float] = None):
        raise NotImplementedError()

    @abstractmethod
    def wait(
        self,
        *,
        timeout: Optional[float] = None,
        condition: Literal["not-running", "next-exit", "removed"] = None,
    ) -> Dict:
        raise NotImplementedError()

    @abstractmethod
    def remove(self, *, v: bool = False, link: bool = False, force: bool = False):
        raise NotImplementedError()

    @abstractmethod
    def is_running(self) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def is_alive(self) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def exec(self, command) -> BaseContainerExec:
        raise NotImplementedError()

    @abstractmethod
    def exec_kill(self, exec_id: str):
        raise NotImplementedError()

    @abstractmethod
    def get_exit_code(self) -> Optional[int]:
        raise NotImplementedError()


class BaseContainerRunner(ABC):
    @abstractmethod
    def prepare_image(self, image: str):
        raise NotImplementedError()

    @abstractmethod
    def spawn_container(
        self,
        image,
        *,
        runtime: str = None,
        entrypoint: List = None,
        detach: bool = True,
        name: str = None,
        remove: bool = False,
        volumes: Dict = None,
        environment: Dict = None,
        labels: Dict = None,
        shm_size: int = None,
        stdin_open: bool = False,
        tty: bool = False,
        cpu_limit: int = None,
        mem_limit: int = None,
        memswap_limit: int = None,
        network: str = None,
        ipc_mode: str = None,
        security_opt: List[str] = None,
    ) -> BaseContainer:
        raise NotImplementedError()
