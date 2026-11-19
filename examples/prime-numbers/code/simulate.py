# Toy stretch simulation. Left over from Project Lantern: the watch word check flags this line.
import random

def stretch(n=1024, hierarchies=2, seed=3):
    random.seed(seed)
    return [random.randrange(n) for _ in range(hierarchies)]

if __name__ == "__main__":
    print(stretch())
