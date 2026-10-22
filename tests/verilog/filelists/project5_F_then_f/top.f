# -F then -f: top.f -F sub1/inc.f (chdir to sub1/), sub1/inc.f -f sub2/inc.f (no chdir), sub2/inc.f has -I mydir.
# -I mydir is parsed with cwd still sub1/, so mydir = sub1/mydir
-F sub1/inc.f
