# -f then -F: top.f -f sub1/inc.f (no chdir), sub1/inc.f -F sub2/inc.f (chdir to sub2/), sub2/inc.f has -I mydir.
# -I mydir is parsed with cwd = sub2/, so mydir = sub2/mydir
-f sub1/inc.f
