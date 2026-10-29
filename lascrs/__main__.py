import sys
if len(sys.argv) > 1:
    from .cli import main
    sys.exit(main())
from .gui import main
main()
