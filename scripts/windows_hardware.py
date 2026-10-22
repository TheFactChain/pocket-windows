"""Display labels for the existing Westmere/IDE Windows hardware; no device changes."""
import re

CPU_MODEL = 'Westmere,model-id=Intel(R) Xeon(R) CPU E5620 @ 2.40GHz'


def arguments(serial=None):
    system = ('type=1,manufacturer=Dell Inc.,product=Precision WorkStation T5500,'
              'family=Precision Workstation,sku=Precision T5500')
    if serial is not None:
        if type(serial) is not str or not re.fullmatch('[A-Za-z0-9-]{1,64}', serial):
            raise ValueError('Invalid Windows hardware serial')
        system += ',serial=' + serial
    # A model label does not change the configured LBA capacity or IDE boot path.
    return ['-smbios', system,
            '-smbios', 'type=2,manufacturer=Dell Inc.,product=Precision T5500',
            '-smbios', 'type=3,manufacturer=Dell Inc.,version=Precision T5500,sku=Precision T5500',
            '-global', 'ide-hd.model=ST3160815AS']
