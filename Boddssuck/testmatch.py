import re

# Example test cases
# inputs are a list of either two or four numbers
# in the case where the input is two numbers, both are integers
# in the case where the input is four numbers, they are two pairs of float, integer in that order.
# in that second case, the input is formatted without any space between the float, integer pairs
# it is safe to assume that each number will have a leading '+' or '-', always
testcases = [
    '+250 -190',  # case one; two integers
    '+2.5 +122-2.5 -108', # case two; float, integer,float, integer
    '-2.5 -123+4.5 +999',
    '-3, +123+5, -98',  # floats will not always have a decimal
    '+4.1234, -2-5.999, +1.11',  # don't make any assumptions about the lengths of each number
]


def SplitNumbers(text):
    splits = re.split('[\+,\-]', text)
    result = [x for x in splits if (x != '' and x != ' ')]
    return result


# Dans dogshit attempt
def ParseNumbers(text):
    # Remove any commas, as they're not needed for parsing
    text = text.replace(',', '')

    # Split the text by the space character to check the number of parts
    parts = text.split()

    # Initialize an empty result list
    result = []

    if len(parts) == 2:  # Case with two integers
        # Convert each part to an integer and add to the result list
        result = [int(parts[0]), int(parts[1])]
    else:  # Case with float, integer pairs
        # Iterate over each part, assuming each part is a float,integer pair without spaces
        for part in parts:
            # Find the index where the float ends and the integer begins
            # We look for the last '+' or '-' sign that is not at the start of the string part
            for i in range(1, len(part)):
                if part[i] in '+-':
                    split_index = i
                    break
            float_part = float(part[:split_index])
            int_part = int(part[split_index:])
            result.append([float_part, int_part])

    return result


for num, test in enumerate(testcases):
    print(f"#{num}")
    #result = ParseNumbers(test)
    result = SplitNumbers(test)
    print(result)
    #assert len(result) == 2

# Test the function with the assertions provided
#assert ParseNumbers(testcases[0]) == [250, -190]
#assert ParseNumbers(testcases[1]) == [[2.5, 122], [-2.5, -108]]
#assert ParseNumbers(testcases[2]) == [[-2.5, -123], [4.5, 999]]

