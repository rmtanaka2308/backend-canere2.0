import sys 
from spleeter.separator import Separator 

separator = Separator('spleeter:2stems')
separator.separate_to_file(sys.argv[1], sys.argv[2])
