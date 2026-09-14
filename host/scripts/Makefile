# Build the host capture helper. stream_grab needs cap_net_raw+cap_net_admin
# to bind-to-device and set a large SO_RCVBUF without sudo:
#   sudo setcap 'cap_net_raw,cap_net_admin+ep' stream_grab
CC     ?= gcc
CFLAGS ?= -O2 -pthread -Wall

all: stream_grab

stream_grab: stream_grab.c
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -f stream_grab

.PHONY: all clean
