# Project 3: Chain of -f and -F. Order: main, then a.f (via -f), then b.f (via -F from a.f), then back.
-D MAIN_FIRST=1
-f a.f
-D MAIN_LAST=2
