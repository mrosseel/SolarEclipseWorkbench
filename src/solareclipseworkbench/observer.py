import abc
import logging


class Observer(abc.ABC):
    @abc.abstractmethod
    def update(self, changed_object):
        pass

    @abc.abstractmethod
    def do(self, actions):
        pass


class Observable:
    def __init__(self):
        self.observers = []

    def add_observer(self, observer):
        if observer not in self.observers:
            self.observers.append(observer)

    def notify_observers(self, changed_object):
        for observer in self.observers:
            try:
                observer.update(changed_object)
            except Exception:
                logging.exception('Observer %s.update() failed', type(observer).__name__)

    def action_observers(self, actions):
        for observer in self.observers:
            try:
                observer.do(actions)
            except Exception:
                logging.exception('Observer %s.do() failed', type(observer).__name__)
