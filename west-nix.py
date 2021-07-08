from west.commands import WestCommand

class Nix(WestCommand):

    def __init__(self):
        super().__init__(
            'nix',  # gets stored as self.name
            'Generate Nix code for fetching manifest sources',  # self.help
            # self.description:
            dedent('''
            '''))

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name,
                                         help=self.help,
                                         description=self.description)

        return parser

    def do_run(self, args, unknown_args):
        pass
