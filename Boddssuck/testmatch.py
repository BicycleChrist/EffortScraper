import more_itertools

# Example test cases
# inputs are a list of either two or four numbers
# in the case where the input is two numbers, both are integers
# in the case where the input is four numbers, they are two pairs of float, integer in that order.
# in that second case, the input is formatted without any space between the float, integer pairs
# it is safe to assume that each number will have a leading '+' or '-', always
testcases = [
    '+250 -190',  # case one; two integers
    '+2.5 +122-2.5 -108',  # case two; float, integer,float, integer
    '-2.5 -123+4.5 +999',
    '-3, +123+5, -98',  # floats will not always have a decimal
    '+4.1234, -2-5.999, +1.11',  # don't make any assumptions about the lengths of each number
]


def SplitNumbers(text):
    splits = [*more_itertools.split_before(text, lambda c: (c.startswith('+') or c.startswith('-')))]
    if len(splits) == 2:
        firstline = str(''.join(splits[0])) + '\n'
        secondline = str(''.join(splits[1]))
    elif len(splits) == 4:
        firstline = str(''.join(splits[0])) + str(''.join(splits[1])) + '\n'
        secondline = str(''.join(splits[2])) + str(''.join(splits[3]))
    else:
        print("unexpected length post-split")
        return []
    return [firstline, secondline]


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

