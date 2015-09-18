import sys
import logging
logger = logging.getLogger('fsm')

class State:
    def __init__(self, name, label = 'blank'):
        self.name = name
        self.label = label
        self.event_handlers = {}
        self.enter_handlers = []

    def add_eventhandler(self, event, tostatename, actions  = []):
        self.event_handlers[event] = [tostatename, actions]
        return self

    def add_enterhandlers(self, actions):
        self.enter_handlers = actions
        return self

    def eventhandler(self, event):
        return self.event_handlers[event]

    def handles(self, event):
        return event in self.event_handlers

    def handle_event(self, event):
        (tostatename, actions) = self.event_handlers[event]
        logger.debug(event+' event: leaving: '+self.name+', entering '+tostatename)
        for action in actions:
            action(event, self.name, tostatename)
        return tostatename
    
    def handle_enter(self, fromstate):
        actions = self.enter_handlers
        logger.debug('enter event'+': left: '+fromstate+', now in: '+self.name)
        for action in actions:
            action('enter', fromstate, self.name)

class Fsm: # Finite State Machine
    def __init__(self):
        self.current_state = None
        self.state_table = {}
        
    def execute(self, event):
        if self.current_state.handles(event):
            tostate = self.current_state.handle_event(event)
            fromstate = self.current_state.name
            self.current_state = self.state_table[tostate]
            self.current_state.handle_enter(fromstate)
        else:
            logger.debug(event + ' not an event for state: ' + self.current_state.name)

    def add_state(self, state):
        self.state_table[state.name] = state

    def get_state(self, state_name):
        if state_name in self.state_table:
            return self.state_table[state_name]
        else:
            return None

    def start(self, state):
        self.current_state = self.state_table[state]

# Test Examples
class Songlist:
    def __init__(self, playlist, curr_song_index=0):
        self.playlist = playlist
        self.song_index = curr_song_index

    def song_title(self):
        song = self.playlist[self.song_index]
        if 'title' in song:
            title = song['title']
        elif 'name' in song:
            title = song['name']
        else:
            title = 'unknown'
        return title

    def select(self, event, prv, nxt):
        if event == 'up':
            self.song_index = max(0, self.song_index-1)
        elif event == 'down':
            self.song_index = min(len(self.playlist), self.song_index+1)
        return self.song_title()

def p_tostatename(event, fromstate, tostate):
    print(event+': leaving: '+fromstate+', entering '+tostate)

if __name__ == '__main__':
    logging.basicConfig(level=logging.DEBUG)
    songlist = Songlist([{'title' : 'song 0'},
                         {'name': 'song 1'},
                         {'title': 'song 2'},
                         {'noop': 'song 3'}])
    FSM = Fsm()
    FSM.add_state(State('idle', 'Idle')
                  .add_enterhandlers([
                      lambda ev, prev, nxt: print(prev+'>Idle>'+nxt),])
                  .add_eventhandler('menu', 'playqueue',[p_tostatename]))
    FSM.add_state(State('playqueue', 'Play Queue')
                  .add_enterhandlers([
                      lambda ev, prev, nxt: print(prev+'>Play Queue>'+nxt),])
                  .add_eventhandler('return', 'idle',[p_tostatename])
                  .add_eventhandler('left', 'idle',[p_tostatename])
                  .add_eventhandler('right', 'songselect', [
                      p_tostatename,
                      lambda ev, prev, nxt: print('fetch and create Songlist'),
                      lambda ev, prev, nxt: print('show: '+songlist.song_title()),
                      ]))
    FSM.add_state(State('songselect', 'Song n')
                  .add_enterhandlers([
                      lambda ev, prev, nxt: print(prev+'>SongSelect>'+nxt),])
                  .add_eventhandler('return', 'playqueue',[p_tostatename])
                  .add_eventhandler('left', 'playqueue',[p_tostatename])
                  .add_eventhandler('up', 'songselect',[
                      p_tostatename,
                      songlist.select,
                      lambda ev, prev, nxt: print('show: '+songlist.song_title())])
                  .add_eventhandler('down', 'songselect',[
                      p_tostatename,
                      songlist.select,
                      lambda ev, prev, nxt: print('show: '+songlist.song_title())])
                  .add_eventhandler('select', 'idle',[
                      p_tostatename,
                      lambda ev, prev, nxt:
                      print('Play Song: '+songlist.song_title())]))
    FSM.start('idle')
    FSM.execute('menu')
    FSM.execute('return')
    FSM.execute('menu')
    FSM.execute('right')
    FSM.execute('down')
    FSM.execute('down')
    FSM.execute('up')
    FSM.execute('down')
    FSM.execute('right')
    FSM.execute('select')
