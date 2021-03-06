#!/usr/bin/env python
import socket
import os
import sys
import time
import subprocess
import logging

import parse
import utils

logging.basicConfig(level=logging.DEBUG)

class YASAClientSession(object):
    def __init__(self, s, db_conn=None):
        self._socket = s
        self._responses = parse.recv_load(self._socket)

        if db_conn:
            self._conn = db_conn
        else:
            self._conn = utils.get_client_connection('yasaclient.db')

    def _send(self, msg):
        totalsent = 0
        while totalsent < len(msg):
            sent = self._socket.send(msg[totalsent:])
            if sent == 0:
                raise RuntimeError("socket connection broken")
            totalsent += sent

    def add_to_itunes(self, path):
        """
        Takes a path to a file and adds it to the iTunes library in whatever
        manner is appropriate for the system. Does NOT delete the file after.
        Returns the new path of the file.
        """
        if sys.platform == 'darwin':
            command = """
                      set f to POSIX file "%s"
                      tell application "iTunes"
                          launch
                          try
                              set t to add (f)
                              set loc to location of t
                              set output to POSIX path of loc
                              do shell script "echo " & quoted form of output
                          end try
                      end tell
                      """

            new_path = subprocess.check_output(["osascript", "-e", 
                                                command % path])

            return new_path.strip()

        elif sys.platform == 'win32':
            pass
        else:
            raise Exception('Unsupported system, aborting.')

    def remove_from_itunes(self, path):
        pass

    def communicate(self, message):
        """
        Takes a structure and sends it to the connected server, then waits for
        the server to send a response and returns the parsed response.
        """
        request = parse.dumps(message)

        logging.debug('GIVE -> %s' % request)

        self._send(request + '\n')
        response = next(self._responses)

        logging.debug('GOT  <- %s' % response)

        return response

    def pull_remote(self, file_id):
        """
        Grabs a file from the remote host and writes its contents to a 
        temporary file. Returns temp file path and file hash (from remote).
        """
        request = parse.dumps({'ACTION': 'PULL-FILE',
                               'ID': file_id})
        self._send(request + '\n')
        file_name = os.tmpnam() + '.mp3'

        digest = utils.pull_file(file_name, self._socket)
        return file_name, digest

    def do_pull(self):
        logging.info('Starting pull process')
        since = (utils.read_settings(self._conn, 'last_update')
                      .get('last_update', 0))
        cursor = self._conn.cursor()
        response = self.communicate({'ACTION': 'PULL',
                                     'SINCE': since})
        to_recv = parse.listify(parse.loads(response['CHANGES']))

        logging.info('Adding %d new files from server.' % len(to_recv))

        for x in to_recv:
            from_serv = parse.loads(x)
            sid = int(from_serv['ID'])
            logging.debug('Proccessing file update. SID: %d, type: %s'
                          % (sid, from_serv['type']))
            if from_serv['type'] == 'NEW':
                cursor.execute('SELECT 1 FROM files WHERE server_id=?', [sid])
                if cursor.fetchone():
                    logging.warning('Server returned a file I already have, '
                                    'ignoring and continuing pull process.')
                    continue

                file_path, file_hash = self.pull_remote(sid)

                fd = open(file_path, 'rb')
                our_hash = utils.hash_file(fd)
                if our_hash.digest() != file_hash:
                    raise Exception('MD5 digests did not match! Transmission '
                                    'error suspected.')

                it_path = self.add_to_itunes(file_path)
                os.remove(file_path)

                record = utils.generate_file_info(it_path)
                record['server_id'] = sid
                utils.insert_file_record(record, self._conn)

                logging.debug('Successfuly added file: %s' 
                              % (os.path.split(it_path)[-1],))

            elif from_serv['type'] == 'DELETE':
                cursor.execute('SELECT * FROM files WHERE server_id=?', [sid])
                record = cursor.fetchone()

                if not record:
                    logging.warning('Server sent delete directive on file I '
                                    'don\'t have. Ignoring.')
                    continue

                self.remove_from_itunes(sid)
                cursor.execute('DELETE FROM files WHERE server_id=?', [sid])

            self._conn.commit()

        logging.info('...finished pull process')

    def do_push(self):
        logging.info('Starting push process')

        cursor = self._conn.cursor()
        since = (utils.read_settings(self._conn, 'last_update')
                      .get('last_update', 0))

        cursor.execute('SELECT * FROM files WHERE server_id IS NULL')
        add_files = cursor.fetchall()

        cursor.execute('SELECT * FROM deleted WHERE del_time>?', [since])
        rem_files = cursor.fetchall()

        logging.info('Notifying server %d new files and %d old ones' %
                     (len(add_files), len(rem_files)))

        for record in add_files:
            response = self.communicate({'ACTION': 'PUSH',
                                         'TYPE': 'NEW'})
            cursor.execute('UPDATE files SET server_id=? WHERE id=?',
                           [int(response['ID']), record['id']])

            utils.push_file(record['path'], self._socket, 
                            hash_code=record['hash'].decode('hex'))

            # Read off the server's reply, since this didn't go through the
            # communicate method we have to do it ourselves.
            next(self._responses)

        for record in rem_files:
            response = self.communicate({'ACTION': 'PUSH',
                                         'TYPE': 'DELETE',
                                         'ID': record['server_id']})

        logging.info('...finished push process')

    def sync(self):
        self.do_pull()
        self.do_push()
        utils.write_settings(self._conn, last_update=time.time())

if __name__ == '__main__':
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(('localhost', 7454))
    sess = YASAClientSession(s)
    sess.sync()
