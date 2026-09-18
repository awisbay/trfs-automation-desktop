import unittest
from terminal_renderer import terminal_text_runs

class TerminalColorTests(unittest.TestCase):
    def test_states_and_valid_ip_preserve_text(self):
        text='  0 (DISABLED) 1 (ENABLED) Connected to 10.42.187.210  '
        runs=terminal_text_runs(text,(255,255,255))
        self.assertEqual(''.join(t for t,c in runs),text)
        self.assertIn(('DISABLED',(255,235,59)),runs)
        self.assertIn(('ENABLED',(80,250,123)),runs)
        self.assertIn(('10.42.187.210',(230,100,255)),runs)

    def test_invalid_ip_and_embedded_states_keep_normal_color(self):
        text='999.1.2.3 NOT_ENABLED DISABLED_FEATURE 25.4.711.28536'
        normal=(240,240,240)
        self.assertEqual(terminal_text_runs(text,normal),[(text,normal)])

if __name__=='__main__':
    unittest.main()
